"""Index state: the constituent set, the divisor, and the level they imply.

    Level_t = SUM_i ( P_it * S_it * F_it * C_it * FX_it ) / D_t

* ``P`` price in local currency
* ``S`` shares in issue
* ``F`` free-float factor (investability, after any foreign ownership limit)
* ``C`` capping factor (an index-design artefact, not a fact about the company)
* ``FX`` local currency into index base currency
* ``D`` the divisor

The divisor is the whole trick. It has no units and no economic meaning: it exists so
that changes in the numerator that are *not* market moves leave the level untouched.
Every methodology question about corporate actions reduces to "does this touch the
divisor?", and `corpactions.engine` answers exactly that.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace

from miniftse.types import EPSILON, Country, Currency, SizeBand


@dataclass(frozen=True, slots=True)
class Constituent:
    """One index member at one instant."""

    security_id: str
    price: float
    shares: float
    free_float_factor: float = 1.0
    capping_factor: float = 1.0
    fx_rate: float = 1.0
    currency: Currency = Currency.USD
    country: Country = Country.US
    icb_industry: str = ""
    adv: float = 0.0
    """Average daily traded value, in index base currency. Drives the capacity
    constraint (`weighting.schemes.capacity_constrained_weights`); not otherwise used
    by the calculation engine."""
    size_band: SizeBand = SizeBand.LARGE
    is_suspended: bool = False

    @property
    def local_market_value(self) -> float:
        return self.price * self.shares * self.free_float_factor * self.capping_factor

    @property
    def market_value(self) -> float:
        """Investable market value in the index base currency."""
        return self.local_market_value * self.fx_rate

    @property
    def full_market_value(self) -> float:
        """Before float and capping. Used for size banding and for the free-float
        divergence checks in the quality layer."""
        return self.price * self.shares * self.fx_rate

    def with_price(self, price: float) -> Constituent:
        return replace(self, price=price)


@dataclass(frozen=True, slots=True)
class IndexState:
    """A complete, immutable snapshot.

    Immutable on purpose. Every event returns a new state, so an incident investigation
    can replay the day one event at a time and see exactly where the level diverged -
    which is not possible if the engine mutates a dict in place.
    """

    date: dt.date
    divisor: float
    constituents: dict[str, Constituent] = field(default_factory=dict)
    base_currency: Currency = Currency.USD

    @property
    def total_market_value(self) -> float:
        return sum(c.market_value for c in self.constituents.values())

    @property
    def level(self) -> float:
        if abs(self.divisor) < EPSILON:
            raise ZeroDivisionError("divisor collapsed to zero - the index is undefined")
        return self.total_market_value / self.divisor

    @property
    def n_constituents(self) -> int:
        return len(self.constituents)

    def weights(self) -> dict[str, float]:
        total = self.total_market_value
        if total <= 0:
            return {}
        return {k: c.market_value / total for k, c in self.constituents.items()}

    def weight_of(self, security_id: str) -> float:
        total = self.total_market_value
        c = self.constituents.get(security_id)
        return c.market_value / total if c and total > 0 else 0.0

    # ---------------------------------------------------------------- mutation

    def replace_constituent(self, constituent: Constituent) -> IndexState:
        new = dict(self.constituents)
        new[constituent.security_id] = constituent
        return replace(self, constituents=new)

    def remove_constituent(self, security_id: str) -> IndexState:
        new = dict(self.constituents)
        new.pop(security_id, None)
        return replace(self, constituents=new)

    def with_divisor(self, divisor: float) -> IndexState:
        return replace(self, divisor=divisor)

    def with_date(self, date: dt.date) -> IndexState:
        return replace(self, date=date)

    # ---------------------------------------------------------------- divisor

    def rebase_divisor(self, market_value_before: float) -> IndexState:
        """Adjust the divisor so the level is unchanged despite a structural change.

            D_new = D_old * (MV_after / MV_before)

        Derived from requiring ``MV_before / D_old == MV_after / D_new``. This single
        line is the entire mechanism behind index continuity, and being able to derive
        it on a whiteboard in ninety seconds is on the interview checklist.
        """
        if market_value_before <= EPSILON:
            # Nothing to preserve continuity against - an index being seeded, or one
            # whose constituents have all been removed on the same day.
            return self
        after = self.total_market_value
        return self.with_divisor(self.divisor * after / market_value_before)

    @classmethod
    def initialise(
        cls,
        date: dt.date,
        constituents: dict[str, Constituent],
        base_level: float = 1000.0,
        base_currency: Currency = Currency.USD,
    ) -> IndexState:
        """Seed an index at a chosen level by solving for the divisor.

        The base level is arbitrary - 100, 1000, whatever the product wants - which is
        precisely why comparing levels across index families is meaningless.
        """
        total = sum(c.market_value for c in constituents.values())
        if total <= 0:
            raise ValueError("cannot initialise an index with zero market value")
        return cls(
            date=date,
            divisor=total / base_level,
            constituents=constituents,
            base_currency=base_currency,
        )


@dataclass(frozen=True, slots=True)
class DivisorChange:
    """An audit record for one divisor movement.

    Kept for every event, not just the interesting ones. When a client asks why the
    level moved 30bp on a day nothing traded, this table is the answer, and being able
    to produce it in minutes rather than hours is the difference between a good client
    response and a bad one.
    """

    date: dt.date
    event_id: str
    event_type: str
    security_id: str
    divisor_before: float
    divisor_after: float
    market_value_before: float
    market_value_after: float
    level_before: float
    level_after: float
    reason: str

    market_value_at_rebase: float | None = None
    """The market value the divisor was asked to preserve.

    Usually the value at the start of the event, but not always. A cash merger first
    marks the target to the deal price - a genuine return, often 10-40% on that name -
    and only then removes it. Continuity applies to the *post-mark* value; measuring
    against the start of the day would report the takeover premium as a defect.
    """

    @property
    def divisor_change_pct(self) -> float:
        return (self.divisor_after / self.divisor_before - 1.0) if self.divisor_before else 0.0

    @property
    def level_at_rebase(self) -> float:
        """The level the rebase was supposed to hold constant."""
        if self.market_value_at_rebase is None or not self.divisor_before:
            return self.level_before
        return self.market_value_at_rebase / self.divisor_before

    @property
    def level_continuity_error_bps(self) -> float:
        """How far the level moved across a change that should have been continuous.

        Should be zero to floating-point precision for every divisor event. The quality
        layer treats anything above a basis point as a blocking defect.
        """
        baseline = self.level_at_rebase
        if not baseline:
            return 0.0
        return (self.level_after / baseline - 1.0) * 10_000

    @property
    def realised_return_bps(self) -> float:
        """Index return genuinely recognised by this event, as distinct from the
        continuity error. Non-zero for dividends, delistings and merger premia."""
        if not self.level_before:
            return 0.0
        return (self.level_at_rebase / self.level_before - 1.0) * 10_000
