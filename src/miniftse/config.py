"""Configuration as code.

Every number an index methodology publishes lives here rather than being scattered
through the calculation. Two reasons, both commercial rather than aesthetic:

1. The Ground Rules document and the code must agree. One source per threshold makes
   that checkable instead of hopeful.
2. Index providers get audited. A published level must be reproducible years later,
   which means the parameter set is part of the run manifest (see `production.manifest`).

Nothing here is a FTSE Russell number. These are miniftse's own choices, and each one
is defended in DECISIONS.md.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Any, Self

from miniftse.types import Country, Currency, SecurityType

# --------------------------------------------------------------------------------------
# Withholding tax
# --------------------------------------------------------------------------------------

DEFAULT_WITHHOLDING_TAX: dict[Country, float] = {
    # Rate applied to dividends in the net total return index. Keyed on the *issuer's*
    # country of domicile. The UK levies no withholding tax on dividends, so for a UK
    # constituent NTR and GTR contributions are identical - a question that comes up in
    # client enquiries more often than you would expect.
    Country.GB: 0.00,
    Country.IE: 0.25,
    Country.US: 0.30,
    Country.DE: 0.26375,
    Country.FR: 0.25,
    Country.JP: 0.15315,
    Country.CH: 0.35,
    Country.CA: 0.25,
    Country.AU: 0.30,
    Country.HK: 0.00,
    Country.SE: 0.30,
    Country.NL: 0.15,
    Country.KR: 0.22,
    Country.KY: 0.00,
    Country.BM: 0.00,
}


@dataclass(frozen=True, slots=True)
class EligibilityConfig:
    """Screens that decide whether a security may enter the index at all."""

    min_free_float_developed: float = 0.05
    """Minimum investable proportion in a developed market."""

    min_free_float_emerging: float = 0.15
    """Emerging markets carry a higher bar: strategic and state ownership is more
    common, and a thin float in an illiquid market is not investable at scale."""

    min_liquidity_turnover: float = 0.0005
    """Median daily traded value as a fraction of free-float market cap, over the
    test window. Screens out names a tracking fund could not build a position in."""

    liquidity_window_days: int = 250

    min_price_observations: int = 200
    """Trading days of price history required in the window. Screens out names with
    long suspensions and recent listings that are not yet eligible for fast entry."""

    min_free_float_market_cap: float = 100_000_000.0
    """In index base currency."""

    excluded_security_types: frozenset[SecurityType] = frozenset(
        {
            SecurityType.WARRANT,
            SecurityType.CONVERTIBLE,
            SecurityType.UNIT_TRUST,
            # Depositary receipts are excluded where the local line is itself eligible:
            # holding both double-counts the issuer. See DECISIONS.md D-014.
            SecurityType.ADR,
            SecurityType.GDR,
        }
    )

    allow_preferred: bool = False
    """Preferred lines are excluded by default. Some markets (Brazil, Korea, Germany)
    have economically significant preferred lines, so this is a live design question
    rather than an obvious call."""


@dataclass(frozen=True, slots=True)
class BandingConfig:
    """Size band boundaries and the buffer zones that stop names oscillating.

    Boundaries are expressed as cumulative percentiles of free-float market cap,
    largest first: Large is the top 70% of the investable universe by value, Mid takes
    it to 85%, Small to 98%, and the tail is Micro.
    """

    large_cutoff: float = 0.70
    mid_cutoff: float = 0.85
    small_cutoff: float = 0.98

    buffer_width: float = 0.02
    """Half-width of the buffer band around each boundary, in the same cumulative-
    percentile units. An incumbent must cross the boundary by this much before it is
    moved. Empirically tuned in the M3 buffer study - see `research/buffer_study`."""

    apply_buffers: bool = True


@dataclass(frozen=True, slots=True)
class CappingConfig:
    """Concentration limits. The 5/10/40 default is the UCITS diversification rule that
    any index used by a European fund must satisfy."""

    max_single_weight: float = 0.10
    """No constituent above 10%."""

    aggregate_threshold: float = 0.05
    aggregate_limit: float = 0.40
    """Constituents individually above 5% must together be at most 40%."""

    max_iterations: int = 100
    tolerance: float = 1e-10
    enabled: bool = True

    def as_ucits_5_10_40(self) -> Self:
        return self

    def as_concentrated_20_35(self) -> Self:
        """The relaxed variant used by concentrated and single-country indices."""
        return replace(self, max_single_weight=0.35, aggregate_threshold=0.20, aggregate_limit=1.0)


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    """Reconstitution calendar.

    The gap between cut-off, announcement and effective date is not administrative
    slack: it is the window in which tracking funds pre-position. Too short and funds
    trade at bad prices; too long and the index is stale and gameable.
    """

    months: tuple[int, ...] = (3, 6, 9, 12)
    """Quarterly review."""

    cutoff_lag_days: int = 21
    """Data cut-off, before the announcement."""

    announcement_lag_days: int = 14
    """Announcement precedes the effective date by this many calendar days."""

    fast_entry_enabled: bool = True
    fast_entry_min_percentile: float = 0.85
    """A new listing joins off-cycle only if it would rank in the top 15% of the
    investable universe. Below that it waits for the next review."""

    fast_entry_lag_days: int = 5

    fast_entry_max_listing_age_days: int = 365
    """Calendar days. Beyond this a security is not a new listing, so a failure to
    qualify is the normal screens working rather than a gap fast entry should fill."""

    intra_review_float_threshold: float = 0.05
    """An absolute change in free float larger than this is implemented immediately
    rather than held to the next review."""

    intra_review_shares_threshold: float = 0.10


@dataclass(frozen=True, slots=True)
class CostConfig:
    """Transaction cost model used for net-of-cost reporting and cost-aware rebalancing."""

    linear_bps: float = 5.0
    """Half-spread plus commission, in basis points of traded value."""

    impact_coefficient: float = 0.10
    """Coefficient on the square-root market impact term: cost = c * sigma * sqrt(q/ADV).
    Square-root rather than linear because it keeps the optimisation convex while
    matching the empirical shape."""

    use_impact: bool = True


@dataclass(frozen=True, slots=True)
class IndexConfig:
    """The complete specification of one index."""

    index_id: str = "MFTSE-GLOBAL"
    name: str = "miniFTSE Global All Cap"
    base_currency: Currency = Currency.USD
    base_date: dt.date = dt.date(2016, 1, 4)
    base_level: float = 1000.0

    eligibility: EligibilityConfig = field(default_factory=EligibilityConfig)
    banding: BandingConfig = field(default_factory=BandingConfig)
    capping: CappingConfig = field(default_factory=CappingConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    costs: CostConfig = field(default_factory=CostConfig)

    withholding_tax: dict[Country, float] = field(
        default_factory=lambda: dict(DEFAULT_WITHHOLDING_TAX)
    )

    size_bands: tuple[str, ...] = ("LARGE", "MID", "SMALL")
    """Which bands this index includes. All Cap takes three; a Large Cap index takes one."""

    def to_dict(self) -> dict[str, Any]:
        """Flat, hashable representation for the run manifest."""
        from dataclasses import asdict

        out = asdict(self)
        out["base_currency"] = str(self.base_currency)
        out["base_date"] = self.base_date.isoformat()
        out["withholding_tax"] = {str(k): v for k, v in sorted(self.withholding_tax.items())}
        out["eligibility"]["excluded_security_types"] = sorted(
            str(s) for s in self.eligibility.excluded_security_types
        )
        return out


# --------------------------------------------------------------------------------------
# Named index variants
# --------------------------------------------------------------------------------------


def global_all_cap() -> IndexConfig:
    """The parent index: everything eligible, float-weighted, 5/10/40 capped."""
    return IndexConfig()


def global_large_mid() -> IndexConfig:
    """The liquid core. Most tracking assets sit in an index shaped like this."""
    return replace(
        global_all_cap(),
        index_id="MFTSE-LARGEMID",
        name="miniFTSE Global Large/Mid Cap",
        size_bands=("LARGE", "MID"),
    )


def developed_only() -> IndexConfig:
    return replace(
        global_all_cap(),
        index_id="MFTSE-DEV",
        name="miniFTSE Developed All Cap",
    )
