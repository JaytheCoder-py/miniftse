"""Client response drafting, with every number computed rather than generated.

The guardrail is the product. A language model asked "why did the index underperform in
2022?" will write a fluent, plausible, confidently-worded paragraph containing numbers
it invented, and the client will act on them. The fix is architectural, not a prompt:

1. Code computes the numbers and puts them in a `FactPack`.
2. The model receives the facts and writes prose around them.
3. `NumberGuard` extracts every numeral from the draft and checks it against the pack.
   Anything unaccounted for blocks the draft.

Step 3 is what makes the system usable in front of clients. It is also mechanical - a
regex and a set-difference - which is the point: the safety property does not depend on
the model behaving.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from miniftse.agents.llm import LlmClient, Message, OfflineLlm

_NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")


@dataclass
class Fact:
    """One computed value, with the provenance that lets a reader check it."""

    key: str
    value: float
    formatted: str
    description: str
    source: str
    """How it was computed - a function name, a query, a run id. Not decoration: the
    first thing a client asks about a surprising number is where it came from."""

    def line(self) -> str:
        return f"{self.key} = {self.formatted}  ({self.description}; source: {self.source})"


@dataclass
class FactPack:
    """The only numbers a draft is allowed to contain."""

    facts: dict[str, Fact] = field(default_factory=dict)
    context: dict[str, str] = field(default_factory=dict)
    """Non-numeric context - index name, period, the client's question."""

    def add(
        self, key: str, value: float, description: str, source: str, fmt: str = "{:.2%}"
    ) -> FactPack:
        self.facts[key] = Fact(
            key=key,
            value=value,
            formatted=fmt.format(value),
            description=description,
            source=source,
        )
        return self

    def add_text(self, key: str, value: str) -> FactPack:
        self.context[key] = value
        return self

    def allowed_numbers(self) -> set[str]:
        """Every numeric token that may legitimately appear in the draft.

        Includes several renderings of each value, because a model asked for 2.34% may
        reasonably write 2.3% or 2%. Rounding is permitted; invention is not.
        """
        allowed: set[str] = set()
        for fact in self.facts.values():
            value = fact.value
            for candidate in (
                fact.formatted,
                f"{value:.4f}",
                f"{value:.3f}",
                f"{value:.2f}",
                f"{value:.1f}",
                f"{value:.0f}",
                f"{value * 100:.2f}",
                f"{value * 100:.1f}",
                f"{value * 100:.0f}",
                f"{value * 10_000:.1f}",
                f"{value * 10_000:.0f}",
                f"{abs(value):.2f}",
                f"{abs(value) * 100:.2f}",
                f"{abs(value) * 100:.1f}",
                f"{abs(value) * 100:.0f}",
                f"{value:,.0f}",
                f"{value:,.2f}",
                f"{abs(value):,.2f}",
            ):
                allowed.update(_NUMBER.findall(candidate))
        for text in self.context.values():
            allowed.update(_NUMBER.findall(text))
        return allowed

    def render(self) -> str:
        lines = ["FACTS (the only numbers you may use):"]
        lines += [f"  {f.line()}" for f in self.facts.values()]
        if self.context:
            lines += ["", "CONTEXT:"]
            lines += [f"  {k}: {v}" for k, v in self.context.items()]
        return "\n".join(lines)


@dataclass
class GuardResult:
    passed: bool
    unverified_numbers: list[str]
    message: str


class NumberGuard:
    """Rejects any draft containing a number the fact pack cannot account for."""

    #: Numbers that are structural rather than factual - years, list markers, ordinary
    #: small integers in prose. Allowing these avoids a guard so noisy it gets disabled,
    #: which is the usual fate of an over-strict check.
    STRUCTURAL = frozenset(
        {str(y) for y in range(1990, 2101)} | {str(n) for n in range(0, 13)} | {"100", "0.0", "1.0"}
    )

    @classmethod
    def check(cls, draft: str, pack: FactPack) -> GuardResult:
        allowed = pack.allowed_numbers() | cls.STRUCTURAL
        found = {n.rstrip(".").lstrip("-") for n in _NUMBER.findall(draft)}
        unverified = sorted(
            n
            for n in found
            if n
            and n not in allowed
            and n.lstrip("-") not in allowed
            and n.replace(",", "") not in allowed
        )
        if unverified:
            return GuardResult(
                passed=False,
                unverified_numbers=unverified,
                message=(
                    f"BLOCKED: the draft contains {len(unverified)} number(s) that do "
                    f"not trace to a computed fact: {', '.join(unverified[:10])}. "
                    "Every figure in a client response must come from a computation, "
                    "not from the model."
                ),
            )
        return GuardResult(True, [], "All numbers trace to computed facts.")


@dataclass
class DraftResponse:
    question: str
    draft: str
    fact_pack: FactPack
    guard: GuardResult
    approved_for_send: bool = False
    """Always False on creation. A client-facing document is sent by a person."""

    def format(self) -> str:
        lines = [
            f"DRAFT RESPONSE - {'PASSED' if self.guard.passed else 'BLOCKED'} number verification",
            "",
            self.draft,
            "",
            "-" * 68,
            self.guard.message,
            "",
            "Facts used:",
        ]
        lines += [f"  {f.line()}" for f in self.fact_pack.facts.values()]
        lines += ["", "NOT SENT. Requires human review and approval."]
        return "\n".join(lines)


SYSTEM_PROMPT = """You draft client responses for an equity index provider.

Absolute rules:

1. Use ONLY the numbers in the FACTS block. Never compute, estimate, round beyond what
   is given, or introduce any other figure. If a number you need is missing, write
   "[figure to be supplied]" and continue.
2. No jargon a pension trustee would not know. If a technical term is unavoidable,
   define it in the same sentence.
3. Be direct about what is not known. "We are still investigating X" is a better
   answer than a confident guess.
4. Never speculate about future performance.
5. Under 300 words unless told otherwise.

Tone: plain, precise, unhurried. You are explaining, not defending."""


@dataclass
class ClientResponseDrafter:
    """Drafts a client response from computed facts."""

    client: LlmClient = field(default_factory=OfflineLlm)

    def draft(self, question: str, pack: FactPack, guidance: str = "") -> DraftResponse:
        prompt = (
            f"{pack.render()}\n\n"
            f"CLIENT QUESTION:\n{question}\n\n"
            + (f"GUIDANCE:\n{guidance}\n\n" if guidance else "")
            + "Draft the response."
        )
        text = self.client.complete(
            [Message("user", prompt)], system=SYSTEM_PROMPT, temperature=0.0
        ).text
        return DraftResponse(
            question=question, draft=text, fact_pack=pack, guard=NumberGuard.check(text, pack)
        )


# --------------------------------------------------------------------------------------
# Fact pack builders - the code half of the system
# --------------------------------------------------------------------------------------


def performance_facts(history: Any, period_days: int = 252) -> FactPack:
    """Facts for a "how has the index performed" enquiry."""
    levels = history.levels
    pack = FactPack()
    pack.add_text("index", history.config.name)
    pack.add_text("as_of", str(levels.iloc[-1]["date"]))

    for column, label in (
        ("price_return", "price return"),
        ("gross_total_return", "gross total return"),
        ("net_total_return", "net total return"),
    ):
        series = levels[column]
        if len(series) > period_days:
            change = float(series.iloc[-1] / series.iloc[-(period_days + 1)] - 1)
            pack.add(
                f"{column}_period",
                change,
                f"{label} over the last {period_days} sessions",
                "IndexHistory.levels",
            )
        pack.add(
            f"{column}_level",
            float(series.iloc[-1]),
            f"current {label} level",
            "IndexHistory.levels",
            fmt="{:,.2f}",
        )

    pack.add(
        "annualised_return",
        history.annualised_return(),
        "annualised gross total return since inception",
        "IndexHistory.annualised_return()",
    )
    pack.add(
        "volatility",
        history.annualised_vol(),
        "annualised volatility of daily returns",
        "IndexHistory.annualised_vol()",
    )
    pack.add(
        "max_drawdown",
        history.max_drawdown(),
        "worst peak-to-trough decline",
        "IndexHistory.max_drawdown()",
    )
    return pack


def attribution_facts(attribution: Any, period: str) -> FactPack:
    """Facts for a "why did it underperform" enquiry."""
    pack = FactPack()
    pack.add_text("period", period)
    pack.add(
        "total_active",
        attribution.total_active,
        "total return relative to the parent index",
        "brinson_fachler()",
    )
    pack.add(
        "allocation",
        attribution.total_allocation,
        "the part explained by sector weighting decisions",
        "brinson_fachler()",
    )
    pack.add(
        "selection",
        attribution.total_selection,
        "the part explained by which securities were held within sectors",
        "brinson_fachler()",
    )
    pack.add(
        "interaction",
        attribution.total_interaction,
        "the combined effect of weighting and selection",
        "brinson_fachler()",
    )

    top = attribution.by_group.head(3)
    for row in top.itertuples(index=False):
        pack.add(
            f"group_{row.group}",
            row.total,
            f"contribution from {row.group}",
            "brinson_fachler().by_group",
        )
    return pack


def turnover_facts(review_outcome: Any, attribution: dict[str, float]) -> FactPack:
    """Facts for a "why did the index turn over so much" enquiry."""
    pack = FactPack()
    pack.add_text("review_date", str(review_outcome.dates.effective))
    pack.add(
        "total_turnover",
        attribution["total"],
        "one-way turnover at this review",
        "turnover_attribution()",
    )
    pack.add(
        "additions",
        attribution["additions"],
        "the part caused by securities joining the index",
        "turnover_attribution()",
    )
    pack.add(
        "deletions",
        attribution["deletions"],
        "the part caused by securities leaving the index",
        "turnover_attribution()",
    )
    pack.add(
        "reweighting",
        attribution["reweighting"],
        "the part caused by reweighting securities held throughout",
        "turnover_attribution()",
    )
    pack.add(
        "n_additions",
        attribution.get("n_additions", 0.0),
        "number of securities added",
        "turnover_attribution()",
        fmt="{:.0f}",
    )
    pack.add(
        "n_deletions",
        attribution.get("n_deletions", 0.0),
        "number of securities removed",
        "turnover_attribution()",
        fmt="{:.0f}",
    )
    return pack


def tracking_difference_facts(
    index_return: float, fund_return: float, components: dict[str, float]
) -> FactPack:
    """Facts for the "your index returned X but our fund returned Y" enquiry.

    The most common client question there is, and the one where an invented number does
    the most damage - the client is already comparing two figures and will notice a
    third that does not add up.
    """
    pack = FactPack()
    pack.add("index_return", index_return, "the published index return", "IndexHistory.levels")
    pack.add("fund_return", fund_return, "the tracking fund's reported return", "client-supplied")
    pack.add(
        "difference",
        fund_return - index_return,
        "total difference between fund and index",
        "computed",
    )
    for name, value in components.items():
        pack.add(
            name,
            value,
            f"the part attributable to {name.replace('_', ' ')}",
            "tracking difference decomposition",
        )
    return pack
