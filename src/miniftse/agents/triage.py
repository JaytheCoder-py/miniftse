"""Data-quality triage agent.

Takes an alert from the validation layer and produces a triage note: likely cause,
the evidence for it, and a suggested action. It never applies a fix - a human approves
anything that touches published data.

The design point worth defending: **the tools do the investigating, the model does the
writing.** Each tool is a deterministic query against real data, and the agent's job is
to choose which to run and then narrate what came back. That split means the evidence in
a triage note is always real even when the model's reasoning about it is wrong - and a
reviewer can check the evidence in seconds.

The hypothesis ranking is deliberately rule-based rather than model-generated. Root
cause analysis on data incidents is a well-trodden decision tree, and encoding it gives
consistent triage, an auditable rationale, and something that works when the model is
unavailable.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from miniftse.agents.llm import LlmClient, Message, OfflineLlm
from miniftse.quality.rules import Finding


@dataclass
class ToolResult:
    tool: str
    arguments: dict[str, Any]
    output: Any
    summary: str

    def format(self) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in self.arguments.items())
        return f"{self.tool}({args}) -> {self.summary}"


@dataclass
class Tool:
    """A deterministic query the agent may run."""

    name: str
    description: str
    run: Callable[..., ToolResult]

    def __call__(self, **kwargs: Any) -> ToolResult:
        return self.run(**kwargs)


@dataclass
class TriageToolkit:
    """The tools available for investigating a data alert.

    Read-only by construction. There is no `fix_price` tool and there should not be:
    the agent's output is a recommendation, and applying it is a separate, human,
    logged action.
    """

    prices: pd.DataFrame
    corp_actions: pd.DataFrame | None = None
    shares: pd.DataFrame | None = None
    weights: pd.Series | None = None
    alternate_prices: pd.DataFrame | None = None
    fx: pd.DataFrame | None = None

    def query_price_history(self, security_id: str, as_of: dt.date, days: int = 10
                            ) -> ToolResult:
        window = self.prices[
            (self.prices["security_id"] == security_id)
            & (self.prices["date"] <= as_of)
        ].sort_values("date").tail(days)
        if window.empty:
            return ToolResult("query_price_history", {"security_id": security_id},
                              None, f"no price history for {security_id}")
        closes = window["close"].to_numpy()
        moves = np.diff(closes) / closes[:-1] if len(closes) > 1 else np.array([])
        return ToolResult(
            "query_price_history",
            {"security_id": security_id, "as_of": str(as_of), "days": days},
            window,
            (
                f"{len(window)} sessions, last close {closes[-1]:.4f}, "
                f"largest one-day move {np.abs(moves).max():+.2%}"
                if moves.size else f"{len(window)} session"
            ),
        )

    def get_corporate_actions(self, security_id: str, as_of: dt.date, window: int = 10
                              ) -> ToolResult:
        if self.corp_actions is None or self.corp_actions.empty:
            return ToolResult("get_corporate_actions", {"security_id": security_id},
                              None, "no corporate action data available")
        start = as_of - dt.timedelta(days=window)
        hits = self.corp_actions[
            (self.corp_actions["security_id"] == security_id)
            & (self.corp_actions["ex_date"] >= start)
            & (self.corp_actions["ex_date"] <= as_of + dt.timedelta(days=window))
        ]
        types = ", ".join(hits["event_type"].astype(str).unique()) or "none"
        return ToolResult(
            "get_corporate_actions",
            {"security_id": security_id, "as_of": str(as_of), "window_days": window},
            hits, f"{len(hits)} event(s) within {window} days: {types}",
        )

    def compare_sources(self, security_id: str, as_of: dt.date) -> ToolResult:
        if self.alternate_prices is None:
            return ToolResult("compare_sources", {"security_id": security_id}, None,
                              "no second source configured")
        primary = self.prices[
            (self.prices["security_id"] == security_id) & (self.prices["date"] == as_of)]
        alt = self.alternate_prices[
            (self.alternate_prices["security_id"] == security_id)
            & (self.alternate_prices["date"] == as_of)]
        if primary.empty or alt.empty:
            return ToolResult("compare_sources", {"security_id": security_id}, None,
                              "one source has no price for this date")
        a, b = float(primary.iloc[0]["close"]), float(alt.iloc[0]["close"])
        return ToolResult(
            "compare_sources", {"security_id": security_id, "as_of": str(as_of)},
            {"primary": a, "alternate": b},
            f"primary {a:.4f} vs alternate {b:.4f} ({a / b - 1:+.2%})",
        )

    def get_index_weight(self, security_id: str) -> ToolResult:
        if self.weights is None:
            return ToolResult("get_index_weight", {"security_id": security_id}, None,
                              "no weights available")
        weight = float(self.weights.get(security_id, 0.0))
        return ToolResult(
            "get_index_weight", {"security_id": security_id}, weight,
            f"index weight {weight:.4%}"
            + (" - immaterial" if weight < 0.0005 else ""),
        )

    def estimate_index_impact(self, security_id: str, price_move: float) -> ToolResult:
        weight = float(self.weights.get(security_id, 0.0)) if self.weights is not None \
            else 0.0
        impact = weight * price_move
        return ToolResult(
            "estimate_index_impact",
            {"security_id": security_id, "price_move": price_move}, impact,
            f"{impact * 10_000:+.2f}bp of index return",
        )

    def check_peers(self, security_id: str, as_of: dt.date, peers: list[str]
                    ) -> ToolResult:
        """Did comparable securities move the same way?

        The single most discriminating test in price triage. If a name is down 30% and
        every peer is down 28%, it is a market event. If every peer is flat, it is a
        data problem. Nothing else separates the two as cheaply.
        """
        prior_dates = sorted(d for d in self.prices["date"].unique() if d < as_of)
        if not prior_dates:
            return ToolResult("check_peers", {"security_id": security_id}, None,
                              "no prior session")
        prior = prior_dates[-1]
        today = self.prices[self.prices["date"] == as_of].set_index("security_id")["close"]
        before = self.prices[self.prices["date"] == prior].set_index("security_id")["close"]
        common = [p for p in peers if p in today.index and p in before.index]
        if not common:
            return ToolResult("check_peers", {"security_id": security_id}, None,
                              "no peers with prices on both dates")
        peer_moves = (today[common] / before[common] - 1.0)
        subject = (float(today.get(security_id, np.nan)) /
                   float(before.get(security_id, np.nan)) - 1.0)
        return ToolResult(
            "check_peers",
            {"security_id": security_id, "as_of": str(as_of), "n_peers": len(common)},
            {"subject": subject, "peer_median": float(peer_moves.median())},
            (
                f"subject {subject:+.2%} against a peer median of "
                f"{peer_moves.median():+.2%} across {len(common)} peers"
            ),
        )

    def as_tools(self) -> dict[str, Tool]:
        return {
            "query_price_history": Tool(
                "query_price_history",
                "Recent closing prices and the largest daily move.",
                self.query_price_history),
            "get_corporate_actions": Tool(
                "get_corporate_actions",
                "Corporate actions around a date, which explain most large moves.",
                self.get_corporate_actions),
            "compare_sources": Tool(
                "compare_sources",
                "Primary feed against a second source - separates a bad feed from a "
                "real move.", self.compare_sources),
            "get_index_weight": Tool(
                "get_index_weight", "Index weight, for materiality.",
                self.get_index_weight),
            "estimate_index_impact": Tool(
                "estimate_index_impact", "Index impact in basis points.",
                self.estimate_index_impact),
            "check_peers": Tool(
                "check_peers", "Whether comparable securities moved the same way.",
                self.check_peers),
        }


@dataclass(frozen=True, slots=True)
class Hypothesis:
    cause: str
    likelihood: str
    evidence: str
    action: str


@dataclass
class TriageNote:
    finding: Finding
    security_id: str | None
    tool_results: list[ToolResult]
    hypotheses: list[Hypothesis]
    recommended_action: str
    materiality_bps: float
    narrative: str
    requires_human: bool = True

    def format(self) -> str:
        lines = [
            f"TRIAGE: {self.finding.rule} ({self.finding.severity.name})",
            f"  {self.finding.message}",
            "",
            f"Subject: {self.security_id or 'index-level'}",
            f"Materiality: {self.materiality_bps:+.2f}bp of index return",
            "",
            "Evidence gathered:",
        ]
        lines += [f"  - {r.format()}" for r in self.tool_results]
        lines += ["", "Ranked hypotheses:"]
        for i, h in enumerate(self.hypotheses, 1):
            lines += [
                f"  {i}. {h.cause}  [{h.likelihood}]",
                f"     evidence: {h.evidence}",
                f"     action:   {h.action}",
            ]
        lines += ["", f"RECOMMENDED: {self.recommended_action}", ""]
        if self.requires_human:
            lines.append("Requires human approval before any correction is applied.")
        if self.narrative:
            lines += ["", "--- draft note ---", self.narrative]
        return "\n".join(lines)


@dataclass
class TriageAgent:
    """Investigates a validation finding and drafts a triage note."""

    toolkit: TriageToolkit
    client: LlmClient = field(default_factory=OfflineLlm)

    def triage(self, finding: Finding, as_of: dt.date,
               peers: list[str] | None = None) -> TriageNote:
        security = self._subject(finding)
        results: list[ToolResult] = []
        tools = self.toolkit.as_tools()

        if security:
            results.append(tools["query_price_history"](security_id=security,
                                                        as_of=as_of))
            results.append(tools["get_corporate_actions"](security_id=security,
                                                          as_of=as_of))
            results.append(tools["get_index_weight"](security_id=security))
            if peers:
                results.append(tools["check_peers"](security_id=security, as_of=as_of,
                                                    peers=peers))
            if self.toolkit.alternate_prices is not None:
                results.append(tools["compare_sources"](security_id=security,
                                                        as_of=as_of))

        hypotheses = self._rank(finding, results)
        materiality = self._materiality(finding, results)
        action = hypotheses[0].action if hypotheses else "escalate to the duty analyst"

        narrative = self.client.complete(
            [Message("user", self._prompt(finding, security, results, hypotheses))],
            system=(
                "You draft internal data-quality triage notes for an index operations "
                "team. Be concise and factual. Use ONLY the evidence given - never "
                "introduce a number that is not in it. State clearly what is known and "
                "what still needs checking."
            ),
        ).text

        return TriageNote(
            finding=finding, security_id=security, tool_results=results,
            hypotheses=hypotheses, recommended_action=action,
            materiality_bps=materiality, narrative=narrative,
            requires_human=finding.severity.name in {"BLOCK", "ESCALATE"},
        )

    @staticmethod
    def _subject(finding: Finding) -> str | None:
        for entry in finding.sample:
            token = str(entry).split()[0].strip(",")
            if token:
                return token
        return None

    def _rank(self, finding: Finding, results: list[ToolResult]) -> list[Hypothesis]:
        """Rule-based hypothesis ranking - the decision tree an analyst follows.

        Encoded rather than delegated to the model so triage is consistent across
        analysts and shifts, the rationale is auditable, and it still works when the
        model is unavailable.
        """
        lookup = {r.tool: r for r in results}
        corp = lookup.get("get_corporate_actions")
        peers = lookup.get("check_peers")
        sources = lookup.get("compare_sources")
        weight = lookup.get("get_index_weight")

        out: list[Hypothesis] = []

        if corp is not None and corp.output is not None and len(corp.output):
            out.append(Hypothesis(
                cause="Unprocessed or mis-timed corporate action",
                likelihood="HIGH",
                evidence=corp.summary,
                action=(
                    "Check the event was applied on the correct ex-date and with the "
                    "correct divisor treatment. Compare the audit trail against the "
                    "event file."
                ),
            ))

        if peers is not None and isinstance(peers.output, dict):
            subject = peers.output["subject"]
            median = peers.output["peer_median"]
            if abs(subject - median) > 0.10:
                out.append(Hypothesis(
                    cause="Bad price on the primary feed",
                    likelihood="HIGH",
                    evidence=(
                        f"the security moved {subject:+.2%} while peers moved "
                        f"{median:+.2%}; a genuine market event would normally move "
                        "comparable names together"
                    ),
                    action="Compare against a second vendor before publication.",
                ))
            else:
                out.append(Hypothesis(
                    cause="Genuine market move",
                    likelihood="HIGH",
                    evidence=(
                        f"peers moved {median:+.2%} against the subject's "
                        f"{subject:+.2%}; the move is consistent with the sector"
                    ),
                    action="No correction. Note the alert and release the block.",
                ))

        if sources is not None and isinstance(sources.output, dict):
            gap = sources.output["primary"] / sources.output["alternate"] - 1
            if abs(gap) > 0.005:
                out.append(Hypothesis(
                    cause="Vendor disagreement on the primary feed",
                    likelihood="HIGH" if abs(gap) > 0.05 else "MEDIUM",
                    evidence=sources.summary,
                    action="Escalate to the data vendor and consider an override.",
                ))

        if finding.rule in {"price_outliers", "positive_prices"} and not out:
            out.append(Hypothesis(
                cause="Data entry or scaling error (factor of 10, wrong currency unit)",
                likelihood="MEDIUM",
                evidence="no corporate action and no peer evidence explains the move",
                action="Inspect the raw vendor record before publication.",
            ))

        if weight is not None and isinstance(weight.output, float) and \
                weight.output < 0.0005:
            out.append(Hypothesis(
                cause="Immaterial regardless of cause",
                likelihood="INFORMATIONAL",
                evidence=weight.summary,
                action=(
                    "Index impact is below the materiality threshold. Log it, fix at "
                    "the next scheduled correction, do not delay publication."
                ),
            ))

        if not out:
            out.append(Hypothesis(
                cause="Unknown",
                likelihood="UNKNOWN",
                evidence="the available tools did not produce a discriminating signal",
                action="Escalate to the duty analyst with the raw vendor record.",
            ))
        return out

    @staticmethod
    def _materiality(finding: Finding, results: list[ToolResult]) -> float:
        if finding.value is not None and finding.rule == "price_outliers":
            return float(finding.value) * 10_000
        weight = next((r for r in results if r.tool == "get_index_weight"), None)
        if weight is not None and isinstance(weight.output, float):
            return weight.output * 10_000
        return 0.0

    @staticmethod
    def _prompt(finding: Finding, security: str | None, results: list[ToolResult],
                hypotheses: list[Hypothesis]) -> str:
        evidence = "\n".join(f"- {r.format()}" for r in results)
        ranked = "\n".join(
            f"{i}. {h.cause} [{h.likelihood}]: {h.evidence}"
            for i, h in enumerate(hypotheses, 1)
        )
        return (
            f"<context>\nAlert: {finding.rule} ({finding.severity.name})\n"
            f"{finding.message}\nSubject: {security or 'index-level'}\n\n"
            f"Evidence:\n{evidence}\n\nRanked hypotheses:\n{ranked}\n</context>\n\n"
            "Draft a short internal triage note for the duty analyst."
        )


def triage_all(
    findings: list[Finding], toolkit: TriageToolkit, as_of: dt.date,
    client: LlmClient | None = None, peers: list[str] | None = None,
) -> list[TriageNote]:
    agent = TriageAgent(toolkit=toolkit, client=client or OfflineLlm())
    return [agent.triage(f, as_of, peers) for f in findings if not f.passed]
