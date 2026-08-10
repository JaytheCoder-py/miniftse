"""Data validation: a rules engine with severities and a publication gate.

At an index provider an error is not a bug, it is a **recalculation event** - with
client notification, a market notice, and in some cases a regulatory obligation. The
economics are therefore asymmetric in a way they are not in most software: publishing a
wrong number is far worse than publishing late. The gate defaults to blocking.

The taxonomy is deliberately layered, because each layer catches things the others
cannot:

* **schema** - types, nullability, primary keys
* **range** - price positive, weight in [0, 1], float factor in [0, 1]
* **cross-field** - market cap equals price times shares
* **temporal** - price jumps, stale prices, gaps in the calendar
* **cross-source** - vendor A against vendor B
* **aggregate** - index level equals the sum of the parts over the divisor
* **reconciliation** - our number against the official one

Severity decides what happens, and the distinction between WARN and BLOCK is a
commercial judgement, not a technical one. A 5-sigma price move on one small constituent
is a warning; the same move on a 4% constituent is a block.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from miniftse.types import Severity


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule's verdict, with enough detail to act on without re-running anything."""

    rule: str
    category: str
    severity: Severity
    passed: bool
    message: str
    n_affected: int = 0
    sample: tuple[str, ...] = ()
    """A handful of affected identifiers. Bounded on purpose - a finding listing 40,000
    securities is not a finding, it is a second problem."""

    value: float | None = None
    threshold: float | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def format(self) -> str:
        icon = {Severity.INFO: "i", Severity.WARN: "!", Severity.BLOCK: "X",
                Severity.ESCALATE: "!!"}[self.severity]
        head = f"[{icon}] {self.rule} ({self.category}): {self.message}"
        if self.sample:
            head += f"\n      e.g. {', '.join(self.sample[:5])}"
        return head


@dataclass
class ValidationReport:
    """Every finding from one run, plus the publication decision."""

    run_id: str
    as_of: dt.date
    findings: list[Finding] = field(default_factory=list)
    started: dt.datetime | None = None
    finished: dt.datetime | None = None

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.passed]

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.failures if f.severity >= Severity.BLOCK]

    @property
    def escalating(self) -> list[Finding]:
        return [f for f in self.failures if f.severity >= Severity.ESCALATE]

    @property
    def may_publish(self) -> bool:
        """The gate. One blocking failure stops publication - no override in code.

        An override belongs to a person with authority to sign for it, recorded in the
        incident log, not to a flag someone can set in a config file at 6am.
        """
        return not self.blocking

    def counts(self) -> dict[str, int]:
        return {
            "total": len(self.findings),
            "passed": sum(f.passed for f in self.findings),
            "warnings": sum(
                1 for f in self.failures if f.severity == Severity.WARN),
            "blocking": len(self.blocking),
            "escalating": len(self.escalating),
        }

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([
            {"rule": f.rule, "category": f.category, "severity": f.severity.name,
             "passed": f.passed, "message": f.message, "n_affected": f.n_affected,
             "sample": ", ".join(f.sample[:5]), "value": f.value,
             "threshold": f.threshold}
            for f in self.findings
        ])

    def summary(self) -> str:
        c = self.counts()
        verdict = "PUBLISH" if self.may_publish else "BLOCKED"
        lines = [
            f"Validation {self.run_id} for {self.as_of}: {verdict}",
            f"  {c['passed']}/{c['total']} checks passed, {c['warnings']} warnings, "
            f"{c['blocking']} blocking, {c['escalating']} escalating",
        ]
        if self.failures:
            lines.append("")
            lines += ["  " + f.format() for f in sorted(
                self.failures, key=lambda f: -f.severity.value)]
        return "\n".join(lines)


# --------------------------------------------------------------------------------------


@dataclass
class Rule:
    """One check. The severity lives with the rule so the policy is data, not code."""

    name: str
    category: str
    severity: Severity
    check: Callable[[ValidationContext], Finding]
    enabled: bool = True
    description: str = ""

    def run(self, context: ValidationContext) -> Finding:
        try:
            return self.check(context)
        except Exception as exc:  # noqa: BLE001
            # A rule that throws is itself a finding. Swallowing the exception would
            # turn a broken check into a silent pass, which is the worst outcome
            # available - the gate would open because the guard fell over.
            return Finding(
                rule=self.name, category=self.category, severity=Severity.BLOCK,
                passed=False,
                message=f"the check itself failed: {type(exc).__name__}: {exc}",
            )


@dataclass
class ValidationContext:
    """Everything the rules can see for one production run."""

    as_of: dt.date
    prices: pd.DataFrame | None = None
    prior_prices: pd.DataFrame | None = None
    shares: pd.DataFrame | None = None
    corp_actions: pd.DataFrame | None = None
    weights: pd.Series | None = None
    constituents: dict[str, Any] = field(default_factory=dict)
    index_level: float | None = None
    prior_index_level: float | None = None
    divisor: float | None = None
    prior_divisor: float | None = None
    total_market_value: float | None = None
    divisor_audit: pd.DataFrame | None = None
    reference: pd.DataFrame | None = None
    alternate_source: pd.DataFrame | None = None
    official_level: float | None = None
    fx: pd.DataFrame | None = None
    prior_fx: pd.DataFrame | None = None
    config: Any = None

    def ok(self, rule: str, category: str, severity: Severity, message: str,
           **kwargs: Any) -> Finding:
        return Finding(rule=rule, category=category, severity=severity, passed=True,
                       message=message, **kwargs)

    def fail(self, rule: str, category: str, severity: Severity, message: str,
             **kwargs: Any) -> Finding:
        return Finding(rule=rule, category=category, severity=severity, passed=False,
                       message=message, **kwargs)


@dataclass
class ValidationEngine:
    """Runs the rule set and produces a report."""

    rules: list[Rule] = field(default_factory=list)

    def add(self, rule: Rule) -> ValidationEngine:
        self.rules.append(rule)
        return self

    def run(self, context: ValidationContext, run_id: str = "adhoc") -> ValidationReport:
        started = dt.datetime.now(dt.UTC)
        findings = [r.run(context) for r in self.rules if r.enabled]
        return ValidationReport(
            run_id=run_id, as_of=context.as_of, findings=findings,
            started=started, finished=dt.datetime.now(dt.UTC),
        )

    def rule_names(self) -> list[str]:
        return [r.name for r in self.rules]

    def catalogue(self) -> pd.DataFrame:
        """The rule set as a table, for the operational runbook."""
        return pd.DataFrame([
            {"rule": r.name, "category": r.category, "severity": r.severity.name,
             "enabled": r.enabled, "description": r.description}
            for r in self.rules
        ])

    @classmethod
    def default(cls) -> ValidationEngine:
        from miniftse.quality.checks import DEFAULT_RULES

        return cls(rules=list(DEFAULT_RULES))


class PublicationGate:
    """The last thing between a computed index and a client.

    Deliberately a separate object rather than a boolean on the report. Publication is
    an action with consequences, so it gets a class, a log line and a recorded decision.
    """

    def __init__(self, engine: ValidationEngine | None = None) -> None:
        self.engine = engine or ValidationEngine.default()
        self.history: list[ValidationReport] = []

    def check(self, context: ValidationContext, run_id: str) -> ValidationReport:
        report = self.engine.run(context, run_id)
        self.history.append(report)
        return report

    def decide(self, report: ValidationReport) -> tuple[bool, str]:
        if report.escalating:
            return False, (
                f"ESCALATE: {len(report.escalating)} check(s) require human sign-off "
                "before publication. Page the duty analyst; do not override in code."
            )
        if report.blocking:
            names = ", ".join(f.rule for f in report.blocking)
            return False, f"BLOCKED by {len(report.blocking)} check(s): {names}"
        warnings = [f for f in report.failures if f.severity == Severity.WARN]
        if warnings:
            return True, (
                f"Published with {len(warnings)} warning(s): "
                f"{', '.join(f.rule for f in warnings)}"
            )
        return True, "Published: all checks passed."
