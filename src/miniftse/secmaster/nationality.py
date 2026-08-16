"""Nationality assignment.

Not a lookup. A company can be incorporated in the Cayman Islands, headquartered in
Shenzhen, primarily listed in Hong Kong, and earn most of its revenue in mainland
China - and its index nationality determines whether it sits in a developed or emerging
index, which in turn determines whether several hundred billion dollars of passive
capital may hold it.

Index providers therefore publish an explicit rule and route the hard cases to a
committee. This module implements a rule of that shape: a weighted evidence test with a
confidence score and an explicit `REVIEW` outcome, because a rule that always returns an
answer is a rule that is silently wrong on the interesting cases.

The design decision worth defending in an interview: the function returns *why*, not
just *what*. An assignment that cannot be explained to a client is not usable, because
the first question after an unexpected classification is always "on what basis?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from miniftse.types import Country, MarketStatus

#: Incorporation domiciles that carry no information about where a company operates.
#: Incorporation there is a tax and legal convenience, so the rule must down-weight it
#: and look at headquarters, listing and revenue instead.
OFFSHORE_DOMICILES: frozenset[Country] = frozenset({Country.KY, Country.BM})

DEVELOPMENT_STATUS: dict[Country, MarketStatus] = {
    Country.US: MarketStatus.DEVELOPED,
    Country.GB: MarketStatus.DEVELOPED,
    Country.DE: MarketStatus.DEVELOPED,
    Country.FR: MarketStatus.DEVELOPED,
    Country.JP: MarketStatus.DEVELOPED,
    Country.CH: MarketStatus.DEVELOPED,
    Country.CA: MarketStatus.DEVELOPED,
    Country.AU: MarketStatus.DEVELOPED,
    Country.NL: MarketStatus.DEVELOPED,
    Country.SE: MarketStatus.DEVELOPED,
    Country.IE: MarketStatus.DEVELOPED,
    Country.HK: MarketStatus.ADVANCED_EMERGING,
    Country.KR: MarketStatus.SECONDARY_EMERGING,
}


class Outcome(StrEnum):
    ASSIGNED = "ASSIGNED"
    REVIEW = "REVIEW"
    """Evidence conflicts or is too thin. Escalate to the committee rather than guess."""


@dataclass(frozen=True, slots=True)
class NationalityEvidence:
    """Everything the rule is allowed to consider."""

    country_of_incorporation: Country
    country_of_headquarters: Country | None = None
    primary_listing_country: Country | None = None
    other_listing_countries: tuple[Country, ...] = ()
    revenue_by_country: dict[Country, float] = field(default_factory=dict)
    """Fractions, need not sum to 1 if some revenue is unallocated."""

    has_local_listing_restriction: bool = False
    """True where local shares are inaccessible to foreign investors, which pushes the
    assignment toward the market where the accessible line trades."""


@dataclass(frozen=True, slots=True)
class NationalityDecision:
    outcome: Outcome
    country: Country | None
    market_status: MarketStatus | None
    confidence: float
    reasons: tuple[str, ...]

    def explain(self) -> str:
        head = (
            f"{self.country} ({self.market_status}), confidence {self.confidence:.0%}"
            if self.country
            else "REFERRED TO COMMITTEE"
        )
        return head + "\n" + "\n".join(f"  - {r}" for r in self.reasons)


#: Weight each piece of evidence carries. Tuned so that no single signal decides on its
#: own except an unambiguous "everything agrees" case, and so an offshore incorporation
#: cannot outvote a genuine headquarters plus listing.
WEIGHTS: dict[str, float] = {
    "incorporation": 0.30,
    "incorporation_offshore": 0.05,
    "headquarters": 0.35,
    "primary_listing": 0.25,
    "revenue": 0.20,
    "secondary_listing": 0.05,
}

REVIEW_THRESHOLD = 0.55
"""Below this the evidence is treated as conflicting and the case is escalated."""


def assign_nationality(evidence: NationalityEvidence) -> NationalityDecision:
    """Score each candidate country and return the winner, or escalate."""
    scores: dict[Country, float] = {}
    reasons: list[str] = []

    def add(country: Country | None, weight: float, why: str) -> None:
        if country is None:
            return
        scores[country] = scores.get(country, 0.0) + weight
        reasons.append(f"{why}: {country} (+{weight:.2f})")

    inc = evidence.country_of_incorporation
    if inc in OFFSHORE_DOMICILES:
        add(inc, WEIGHTS["incorporation_offshore"], "incorporation (offshore, discounted)")
        reasons.append(
            f"{inc} is an offshore domicile of convenience, so incorporation is weak "
            "evidence of where the company actually is"
        )
    else:
        add(inc, WEIGHTS["incorporation"], "incorporation")

    add(evidence.country_of_headquarters, WEIGHTS["headquarters"], "headquarters")
    add(evidence.primary_listing_country, WEIGHTS["primary_listing"], "primary listing")

    for c in evidence.other_listing_countries:
        add(c, WEIGHTS["secondary_listing"], "secondary listing")

    if evidence.revenue_by_country:
        top, share = max(evidence.revenue_by_country.items(), key=lambda kv: kv[1])
        if share >= 0.50:
            add(top, WEIGHTS["revenue"] * min(share, 1.0), f"revenue concentration ({share:.0%})")
        else:
            reasons.append(
                f"revenue is dispersed (largest is {top} at {share:.0%}), so it carries no weight"
            )

    if evidence.has_local_listing_restriction and evidence.primary_listing_country:
        add(
            evidence.primary_listing_country,
            0.10,
            "local line inaccessible to foreign investors, so the accessible listing "
            "governs investability",
        )

    if not scores:
        return NationalityDecision(
            Outcome.REVIEW, None, None, 0.0, ("no usable evidence supplied",)
        )

    total = sum(scores.values())
    winner, top_score = max(scores.items(), key=lambda kv: kv[1])
    confidence = top_score / total if total else 0.0

    ordered = sorted(scores.values(), reverse=True)
    margin = ordered[0] - (ordered[1] if len(ordered) > 1 else 0.0)

    if confidence < REVIEW_THRESHOLD or margin < 0.10:
        reasons.append(
            f"top candidate {winner} scores {confidence:.0%} with a margin of "
            f"{margin:.2f} over the runner-up - below the {REVIEW_THRESHOLD:.0%} "
            "threshold, so this is a committee decision, not a rule decision"
        )
        return NationalityDecision(Outcome.REVIEW, None, None, confidence, tuple(reasons))

    return NationalityDecision(
        outcome=Outcome.ASSIGNED,
        country=winner,
        market_status=DEVELOPMENT_STATUS.get(winner, MarketStatus.FRONTIER),
        confidence=confidence,
        reasons=tuple(reasons),
    )


def market_status_for(country: Country) -> MarketStatus:
    return DEVELOPMENT_STATUS.get(country, MarketStatus.FRONTIER)
