"""The index calculator: the daily production loop.

Each trading day, in this order:

1. Roll constituent prices to today's close, converting to base currency.
2. Apply every corporate action with today's ex-date, in the order defined by
   `corpactions.events._APPLY_ORDER`, rebasing the divisor for structural events.
3. If today is a review effective date, swap in the new constituent set - also a
   structural change, so again the divisor absorbs it.
4. Publish the price level, and roll the total return levels forward using index
   dividend points.

Total return uses the dividend-points identity rather than a second divisor::

    TR_t / TR_{t-1} = (PR_t + D_t) / PR_{t-1},    D_t = cash distributed / divisor

Equivalent to maintaining separate divisors, but it keeps one divisor as the single
source of truth, which means the audit trail cannot disagree with itself. Dividends
reinvest on the **ex-date**, not the pay date - the convention that catches people out.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import pandas as pd

from miniftse.calc.fx import FxTable
from miniftse.calc.state import Constituent, DivisorChange, IndexState
from miniftse.config import IndexConfig
from miniftse.corpactions.engine import CorporateActionEngine
from miniftse.corpactions.events import CorporateAction, parse_events
from miniftse.types import Country, Currency, SizeBand


class IndexCalculationError(RuntimeError):
    """The index could not be calculated for a date. Never silently skipped: a missing
    day in a published series is a client-visible defect."""


@dataclass(frozen=True, slots=True)
class ConstituentSpec:
    """What a review decides: membership, share count, float and capping factor.

    Deliberately carries no price. Prices arrive daily; this is the part that only
    changes at a review or on a corporate action, and separating them is what makes
    "the index changed but nothing traded" answerable.
    """

    security_id: str
    shares: float
    free_float_factor: float
    capping_factor: float = 1.0
    size_band: SizeBand = SizeBand.LARGE
    currency: Currency = Currency.USD
    country: Country = Country.US
    icb_industry: str = ""


class UniverseSource(Protocol):
    """Supplies the constituent set at each review effective date."""

    def effective_dates(self, start: dt.date, end: dt.date) -> list[dt.date]: ...

    def constituents_for(self, as_of: dt.date) -> dict[str, ConstituentSpec]: ...


@dataclass
class IndexHistory:
    """Everything a run produced, in the shape the reporting layer wants."""

    levels: pd.DataFrame
    """date, price_return, gross_total_return, net_total_return, divisor,
    n_constituents, total_market_value, dividend_points."""

    weights: pd.DataFrame
    """Long: date, security_id, weight. Snapshotted on review dates and month ends
    rather than daily - a daily weight panel is large and almost never the question."""

    divisor_audit: pd.DataFrame
    reviews: pd.DataFrame
    config: IndexConfig
    warnings: list[str] = field(default_factory=list)

    def returns(self, kind: str = "gross_total_return") -> pd.Series:
        s = self.levels.set_index("date")[kind]
        return s.pct_change().dropna()

    def annualised_return(self, kind: str = "gross_total_return") -> float:
        s = self.levels.set_index("date")[kind]
        years = (s.index[-1] - s.index[0]).days / 365.25
        return float((s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1) if years > 0 else 0.0

    def annualised_vol(self, kind: str = "gross_total_return") -> float:
        return float(self.returns(kind).std() * np.sqrt(252))

    def max_drawdown(self, kind: str = "gross_total_return") -> float:
        s = self.levels.set_index("date")[kind]
        return float((s / s.cummax() - 1).min())

    def summary(self) -> dict[str, float | int | str]:
        return {
            "index_id": self.config.index_id,
            "start": str(self.levels["date"].iloc[0]),
            "end": str(self.levels["date"].iloc[-1]),
            "days": len(self.levels),
            "final_pr": float(self.levels["price_return"].iloc[-1]),
            "final_gtr": float(self.levels["gross_total_return"].iloc[-1]),
            "final_ntr": float(self.levels["net_total_return"].iloc[-1]),
            "ann_return_gtr": self.annualised_return(),
            "ann_vol": self.annualised_vol(),
            "max_drawdown": self.max_drawdown(),
            "mean_constituents": float(self.levels["n_constituents"].mean()),
            "divisor_events": len(self.divisor_audit),
            "reviews": len(self.reviews),
        }


@dataclass
class IndexCalculator:
    """Runs an index forward through time."""

    config: IndexConfig
    fx: FxTable
    engine: CorporateActionEngine = field(default_factory=CorporateActionEngine)
    snapshot_weights: Callable[[dt.date], bool] | None = None
    """Predicate deciding which days get a weight snapshot. Defaults to month ends and
    review dates."""

    max_stale_sessions: int = 20
    """Sessions without a price before a constituent is dropped at the next review.

    Matches Ground Rules §5.7: a suspension beyond twenty trading days stops being a
    suspension and becomes a valuation question. Below the threshold the last price is
    carried, which is the correct treatment for a security that will resume trading."""

    _warnings: list[str] = field(default_factory=list, repr=False)

    final_state: IndexState | None = field(default=None, repr=False)
    """The closing state of the last run.

    Retained so a daily job can persist exactly what the calculation produced. Rebuilding
    the constituent set from the last review's specs instead is subtly wrong: the
    calculator drops securities that have stopped trading, and re-deriving reinstates
    them at a carried price."""

    def run(
        self,
        prices: pd.DataFrame,
        corp_actions: pd.DataFrame,
        universe: UniverseSource,
        start: dt.date,
        end: dt.date,
    ) -> IndexHistory:
        """Calculate the index over a date range."""
        price_book = _PriceBook(prices)
        calendar = price_book.dates_between(start, end)
        if not calendar:
            raise IndexCalculationError(f"no price data between {start} and {end}")

        review_dates = set(universe.effective_dates(start, end))
        events_by_date = _index_events(corp_actions)

        state = self._seed(calendar[0], universe, price_book)
        pr_prev = state.level
        gtr = ntr = self.config.base_level
        pr_series: list[dict[str, object]] = []
        weight_rows: list[dict[str, object]] = []
        review_rows: list[dict[str, object]] = []

        for i, date in enumerate(calendar):
            # --- 1. mark to market ------------------------------------------------
            state = self._mark(state, date, price_book)

            # --- 2. corporate actions ---------------------------------------------
            gross_cash = net_cash = 0.0
            todays = events_by_date.get(date)
            if todays:
                state, gross_cash, net_cash, _ = self.engine.apply_all(todays, state)

            # --- 3. review ---------------------------------------------------------
            if date in review_dates and i > 0:
                state, review_row = self._apply_review(state, date, universe, price_book)
                review_rows.append(review_row)

            if not state.constituents:
                raise IndexCalculationError(f"index emptied on {date}")

            # --- 4. publish ---------------------------------------------------------
            pr = state.level
            div_points = gross_cash / state.divisor if state.divisor else 0.0
            net_points = net_cash / state.divisor if state.divisor else 0.0

            if i > 0 and pr_prev > 0:
                gtr *= (pr + div_points) / pr_prev
                ntr *= (pr + net_points) / pr_prev

            pr_series.append({
                "date": date,
                "price_return": pr,
                "gross_total_return": gtr,
                "net_total_return": ntr,
                "divisor": state.divisor,
                "n_constituents": state.n_constituents,
                "total_market_value": state.total_market_value,
                "dividend_points": div_points,
                "net_dividend_points": net_points,
            })
            pr_prev = pr

            if self._should_snapshot(date, calendar, i, review_dates):
                weight_rows.extend(
                    {"date": date, "security_id": k, "weight": v}
                    for k, v in state.weights().items()
                )

        self.final_state = state
        return IndexHistory(
            levels=pd.DataFrame(pr_series),
            weights=pd.DataFrame(weight_rows),
            divisor_audit=self.engine.audit_frame(),
            reviews=pd.DataFrame(review_rows),
            config=self.config,
            warnings=list(self._warnings),
        )

    # ------------------------------------------------------------------ internals

    def _seed(
        self, date: dt.date, universe: UniverseSource, price_book: _PriceBook
    ) -> IndexState:
        specs = universe.constituents_for(date)
        constituents = {}
        for sec_id, spec in specs.items():
            price = price_book.price(sec_id, date)
            if price is None:
                continue
            constituents[sec_id] = self._to_constituent(spec, price, date)
        if not constituents:
            raise IndexCalculationError(f"no priced constituents on the base date {date}")
        return IndexState.initialise(
            date, constituents, self.config.base_level, self.config.base_currency
        )

    def _to_constituent(
        self, spec: ConstituentSpec, price: float, date: dt.date
    ) -> Constituent:
        return Constituent(
            security_id=spec.security_id,
            price=price,
            shares=spec.shares,
            free_float_factor=spec.free_float_factor,
            capping_factor=spec.capping_factor,
            fx_rate=self.fx.rate(date, str(spec.currency)),
            currency=spec.currency,
            country=spec.country,
            icb_industry=spec.icb_industry,
            size_band=spec.size_band,
        )

    def _mark(self, state: IndexState, date: dt.date, price_book: _PriceBook) -> IndexState:
        """Roll prices and FX forward. A pure market move, so the divisor never changes
        here - which is the invariant the property tests check."""
        updated: dict[str, Constituent] = {}
        for sec_id, c in state.constituents.items():
            price = price_book.price(sec_id, date)
            if price is None:
                # No print today. Carrying the previous close is the standard treatment
                # for a holiday or a suspension; a genuinely stale feed is caught by the
                # quality layer, not silently smoothed over here.
                price = c.price
            updated[sec_id] = Constituent(
                security_id=c.security_id, price=price, shares=c.shares,
                free_float_factor=c.free_float_factor, capping_factor=c.capping_factor,
                fx_rate=self.fx.rate(date, str(c.currency)), currency=c.currency,
                country=c.country, icb_industry=c.icb_industry, size_band=c.size_band,
                is_suspended=price_book.is_suspended(sec_id, date),
            )
        return IndexState(date=date, divisor=state.divisor, constituents=updated,
                          base_currency=state.base_currency)

    def _apply_review(
        self,
        state: IndexState,
        date: dt.date,
        universe: UniverseSource,
        price_book: _PriceBook,
    ) -> tuple[IndexState, dict[str, object]]:
        """Swap in the new constituent set.

        Structurally identical to a corporate action: market value changes for reasons
        that are not price moves, so the divisor absorbs the change and the level is
        continuous across the review. A tracking fund trades; the index does not jump.
        """
        mv_before = state.total_market_value
        level_before = state.level
        old_weights = state.weights()

        specs = universe.constituents_for(date)
        new_constituents: dict[str, Constituent] = {}
        for sec_id, spec in specs.items():
            price = price_book.price(sec_id, date)
            if price is None:
                # No print today. Distinguish a short suspension, where the standard
                # treatment is to carry the last price, from a security that has simply
                # stopped trading.
                #
                # Screens are run on cut-off data, so a security that delisted between
                # the cut-off and the effective date still passes them. Without this
                # check it enters - or stays in - the index at a carried price and
                # never leaves, because the delisting event fired on a day it was not a
                # constituent and was skipped. The `constituents_priced` validation rule
                # caught exactly this on a live build.
                stale_sessions = price_book.sessions_since_price(sec_id, date)
                if stale_sessions > self.max_stale_sessions:
                    self._warnings.append(
                        f"{date}: {sec_id} dropped at review - no price for "
                        f"{stale_sessions} sessions"
                    )
                    continue
                existing = state.constituents.get(sec_id)
                if existing is None:
                    self._warnings.append(
                        f"{date}: {sec_id} selected at review but has no price; skipped"
                    )
                    continue
                price = existing.price
            new_constituents[sec_id] = self._to_constituent(spec, price, date)

        if not new_constituents:
            raise IndexCalculationError(f"review on {date} produced an empty universe")

        new_state = IndexState(date=date, divisor=state.divisor,
                               constituents=new_constituents,
                               base_currency=state.base_currency).rebase_divisor(mv_before)

        new_weights = new_state.weights()
        keys = set(old_weights) | set(new_weights)
        turnover = sum(
            abs(new_weights.get(k, 0.0) - old_weights.get(k, 0.0)) for k in keys
        ) / 2.0
        additions = sorted(set(new_weights) - set(old_weights))
        deletions = sorted(set(old_weights) - set(new_weights))

        self.engine.audit.append(DivisorChange(
            date=date, event_id=f"REVIEW-{date.isoformat()}", event_type="REVIEW",
            security_id="*", divisor_before=state.divisor,
            divisor_after=new_state.divisor, market_value_before=mv_before,
            market_value_after=new_state.total_market_value,
            level_before=level_before, level_after=new_state.level,
            reason=f"periodic review: {len(additions)} in, {len(deletions)} out",
        ))

        return new_state, {
            "date": date,
            "n_before": len(old_weights),
            "n_after": len(new_weights),
            "n_additions": len(additions),
            "n_deletions": len(deletions),
            "one_way_turnover": turnover,
            "additions_weight": sum(new_weights.get(k, 0.0) for k in additions),
            "deletions_weight": sum(old_weights.get(k, 0.0) for k in deletions),
            "divisor_before": state.divisor,
            "divisor_after": new_state.divisor,
            "level_continuity_bps": (new_state.level / level_before - 1) * 10_000,
        }

    def _should_snapshot(
        self, date: dt.date, calendar: Sequence[dt.date], i: int, review_dates: set[dt.date]
    ) -> bool:
        if self.snapshot_weights is not None:
            return self.snapshot_weights(date)
        if date in review_dates or i == 0 or i == len(calendar) - 1:
            return True
        return i + 1 < len(calendar) and calendar[i + 1].month != date.month


# --------------------------------------------------------------------------------------


class _PriceBook:
    """Date-and-security price lookup, built once per run.

    A dict of dicts rather than a pivoted frame: the panel is ragged, most securities
    are absent on most early dates, and a dense matrix of 500 x 2900 mostly-NaN floats
    is both slower to index and larger than the sparse form.
    """

    def __init__(self, prices: pd.DataFrame) -> None:
        self._by_date: dict[dt.date, dict[str, float]] = {}
        self._suspended: dict[dt.date, set[str]] = {}
        for row in prices.itertuples(index=False):
            d = _as_date(row.date)
            self._by_date.setdefault(d, {})[str(row.security_id)] = float(row.close)
            if getattr(row, "is_suspended", False):
                self._suspended.setdefault(d, set()).add(str(row.security_id))
        self._dates = sorted(self._by_date)

    def dates_between(self, start: dt.date, end: dt.date) -> list[dt.date]:
        return [d for d in self._dates if start <= d <= end]

    def price(self, security_id: str, date: dt.date) -> float | None:
        return self._by_date.get(date, {}).get(security_id)

    def is_suspended(self, security_id: str, date: dt.date) -> bool:
        return security_id in self._suspended.get(date, set())

    def prices_on(self, date: dt.date) -> dict[str, float]:
        return self._by_date.get(date, {})

    def last_price_before(self, security_id: str, date: dt.date) -> float | None:
        for d in reversed([x for x in self._dates if x < date]):
            hit = self._by_date[d].get(security_id)
            if hit is not None:
                return hit
        return None

    def sessions_since_price(self, security_id: str, date: dt.date) -> int:
        """Trading sessions since this security last printed a price.

        Zero if it traded today. Used at review to drop securities that have stopped
        trading altogether, as distinct from ones merely suspended for a few days.
        """
        count = 0
        for d in reversed([x for x in self._dates if x <= date]):
            if self._by_date[d].get(security_id) is not None:
                return count
            count += 1
        return count


def _index_events(corp_actions: pd.DataFrame) -> dict[dt.date, list[CorporateAction]]:
    if corp_actions is None or corp_actions.empty:
        return {}
    events = parse_events(corp_actions)
    out: dict[dt.date, list[CorporateAction]] = {}
    for e in events:
        out.setdefault(e.ex_date, []).append(e)
    return out


def _as_date(value: object) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.Timestamp(value).date()  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Return decomposition
# --------------------------------------------------------------------------------------


def decompose_total_return(history: IndexHistory) -> pd.DataFrame:
    """Split total return into price return and income, and check they reconcile.

    ``GTR - PR`` over a year should equal the dividend yield contribution. If it does
    not, either dividends are being reinvested on the wrong date or some are being
    counted twice - both of which are silent until someone compares against a published
    series.
    """
    lv = history.levels.set_index("date")
    out = pd.DataFrame({
        "price_return": lv["price_return"].pct_change(),
        "gross_total_return": lv["gross_total_return"].pct_change(),
        "net_total_return": lv["net_total_return"].pct_change(),
        "dividend_points": lv["dividend_points"],
    })
    out["income_contribution"] = out["gross_total_return"] - out["price_return"]
    out["withholding_drag"] = out["gross_total_return"] - out["net_total_return"]
    return out.dropna()


def annual_income_check(history: IndexHistory) -> pd.DataFrame:
    """Yearly GTR minus PR, which should look like a plausible dividend yield.

    Between roughly 1.5% and 4% for a developed global universe. Outside that range,
    the total return calculation is wrong - a cheap, powerful sanity check that costs
    one line and catches a whole class of defect.
    """
    lv = history.levels.copy()
    lv["date"] = pd.to_datetime(lv["date"])
    lv = lv.set_index("date")
    yearly = lv[["price_return", "gross_total_return", "net_total_return"]].resample(
        "YE"
    ).last()
    rets = yearly.pct_change().dropna()
    rets["income_yield"] = rets["gross_total_return"] - rets["price_return"]
    rets["withholding_cost"] = rets["gross_total_return"] - rets["net_total_return"]
    rets["plausible"] = rets["income_yield"].between(0.005, 0.06)
    return rets
