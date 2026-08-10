"""Assemble `FactorInputs` from a market data provider, point-in-time.

The whole value of this module is the PIT discipline. It is easy to compute a factor
correctly and still get a fictitious backtest because the inputs came from a query that
did not bound `filed_date`. Every fundamental here goes through the provider's PIT
interface, and the trailing-twelve-month sums collapse restatements to the latest filing
*known at the time*.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from miniftse.factors.definitions import ALL_FACTORS, FactorInputs

FLOW_ITEMS = frozenset({
    "NET_INCOME", "REVENUE", "GROSS_PROFIT", "OPERATING_CASHFLOW", "CAPEX",
    "DIVIDENDS_PAID",
})
"""Income-statement and cash-flow items, which must be summed over four quarters.
Balance-sheet items are levels and must not be - summing four quarters of total assets
gives four times the company."""


@dataclass
class FactorInputBuilder:
    """Builds one cross-section of factor inputs at a time."""

    provider: object
    base_currency: str = "USD"
    return_window: int = 300
    """Trading days of return history to carry, enough for a 12-month momentum window
    plus the one-month skip."""

    def build(
        self,
        as_of: dt.date,
        security_ids: list[str] | None = None,
        fx_rates: dict[str, float] | None = None,
    ) -> FactorInputs:
        self._current_as_of = as_of
        prices = self._prices_upto(as_of)
        if prices.empty:
            raise ValueError(f"no price data on or before {as_of}")

        latest = (
            prices.sort_values("date").groupby("security_id").last()
        )
        if security_ids is not None:
            latest = latest[latest.index.isin(security_ids)]
        ids = list(latest.index)

        fx_rates = fx_rates or {}
        fx = latest["currency"].map(lambda c: fx_rates.get(str(c), 1.0))

        shares = self.provider.get_shares(ids, as_of)  # type: ignore[attr-defined]
        shares = shares.set_index("security_id")
        shares_out = shares["shares_outstanding"].reindex(ids)
        float_factor = np.minimum(
            shares["free_float_factor"].reindex(ids).fillna(1.0),
            shares.get("foreign_ownership_limit",
                       pd.Series(1.0, index=shares.index)).reindex(ids).fillna(1.0),
        )

        price = latest["close"].reindex(ids)
        market_cap = price * shares_out * fx
        float_cap = market_cap * float_factor

        classifications = self.provider.get_classifications(ids, as_of)  # type: ignore[attr-defined]
        industry = classifications.set_index("security_id")["icb_industry"].reindex(ids)
        securities = self.provider.get_securities()  # type: ignore[attr-defined]
        country = securities.set_index("security_id")["country"].reindex(ids)

        fundamentals = self._fundamentals(ids, as_of, fx_rates, latest["currency"])
        prior = self._fundamentals(
            ids, as_of - dt.timedelta(days=365), fx_rates, latest["currency"]
        )
        returns = self._returns(prices, ids)

        return FactorInputs(
            as_of=as_of, price=price, market_cap=market_cap,
            float_market_cap=float_cap, industry=industry, country=country,
            fundamentals=fundamentals, returns=returns, prior_fundamentals=prior,
        )

    # ------------------------------------------------------------------ internals
    #
    # Everything below is cached across dates. Building a score panel means calling
    # `build` once per month for a decade, and re-pivoting a half-million-row price
    # frame each time turned a two-minute job into half an hour. The caches are keyed
    # on nothing - the underlying tables are immutable for the life of a run - so this
    # is safe as long as the provider is not swapped underneath the builder.

    _price_cache: pd.DataFrame | None = None
    _wide_close: pd.DataFrame | None = None
    _fund_cache: pd.DataFrame | None = None

    def _prices_upto(self, as_of: dt.date) -> pd.DataFrame:
        if self._price_cache is None:
            self._price_cache = self.provider.get_prices(  # type: ignore[attr-defined]
                None, dt.date(1990, 1, 1), dt.date(2100, 1, 1)
            ).sort_values(["security_id", "date"])
        df = self._price_cache
        window_start = as_of - dt.timedelta(days=int(self.return_window * 1.55))
        return df[(df["date"] <= as_of) & (df["date"] > window_start)]

    def _wide(self) -> pd.DataFrame:
        if self._wide_close is None:
            if self._price_cache is None:
                self._prices_upto(dt.date(2100, 1, 1))
            assert self._price_cache is not None
            self._wide_close = self._price_cache.pivot_table(
                index="date", columns="security_id", values="close", aggfunc="last"
            ).sort_index()
        return self._wide_close

    def _fundamentals(
        self,
        ids: list[str],
        as_of: dt.date,
        fx_rates: dict[str, float],
        currencies: pd.Series,
    ) -> pd.DataFrame:
        """Latest balance-sheet levels and trailing-twelve-month flows, PIT correct."""
        items = sorted({s for f in ALL_FACTORS.values() for s in _items_used(f)})
        if self._fund_cache is None:
            self._fund_cache = self.provider.get_fundamentals_raw()  # type: ignore[attr-defined]
        known = self._fund_cache[self._fund_cache["filed_date"] <= as_of]
        known = known[known["item"].isin(items)]
        # Collapse restatements: one row per (security, item, period), the latest filing
        # known on `as_of`. Skipping this double-counts every restated period.
        known = (
            known.sort_values("filed_date")
            .drop_duplicates(["security_id", "item", "period_end"], keep="last")
        )
        stale_cutoff = as_of - dt.timedelta(days=550)
        known = known[known["period_end"] >= stale_cutoff]

        out: dict[str, pd.Series] = {}
        for item in items:
            sub = known[known["item"] == item]
            if sub.empty:
                out[item] = pd.Series(np.nan, index=ids)
                continue
            if item in FLOW_ITEMS:
                # Flows accumulate: sum the last four quarters.
                ranked = sub.sort_values(["security_id", "period_end"])
                top4 = ranked.groupby("security_id").tail(4)
                agg = top4.groupby("security_id").agg(v=("value", "sum"),
                                                      n=("value", "size"))
                series = agg.loc[agg["n"] == 4, "v"]
            else:
                # Levels do not: take the most recent balance sheet only.
                series = (
                    sub.sort_values("period_end").groupby("security_id")["value"].last()
                )
            out[item] = series.reindex(ids)

        frame = pd.DataFrame(out, index=ids)
        # Fundamentals are reported in the company's own currency; market cap is in
        # base. Comparing the two without converting is a silent error that scales with
        # the exchange rate - a JPY-reporting company looks a hundred times cheaper.
        fx = currencies.reindex(ids).map(lambda c: fx_rates.get(str(c), 1.0))
        return frame.mul(fx, axis=0)

    def _returns(self, prices: pd.DataFrame, ids: list[str]) -> pd.DataFrame:
        del prices  # served from the cached wide matrix instead
        wide = self._wide()
        window = wide.loc[wide.index <= self._current_as_of].tail(self.return_window)
        return window.reindex(columns=ids).pct_change()

    _current_as_of: dt.date = dt.date(2100, 1, 1)


def _items_used(factor: object) -> set[str]:
    """Fundamental items a factor's sub-signals reference.

    Derived from the source of the compute functions rather than declared separately,
    so adding a sub-signal cannot silently fail to load its data.
    """
    import inspect
    import re

    items: set[str] = set()
    for sub in factor.sub_signals:  # type: ignore[attr-defined]
        try:
            src = inspect.getsource(sub.compute)
        except (OSError, TypeError):
            continue
        items.update(re.findall(r'item\("([A-Z_]+)"\)', src))
        items.update(re.findall(r'prior_item\("([A-Z_]+)"\)', src))
    return items


def build_score_panels(
    builder: FactorInputBuilder,
    dates: list[dt.date],
    factors: list[str],
    fx_rates: dict[str, float] | None = None,
) -> dict[str, pd.DataFrame]:
    """Score panels for several factors, building each cross-section only once.

    Calling `build_score_panel` per factor rebuilds the same `FactorInputs` object once
    per factor per date, and constructing that object - the PIT fundamental join and the
    return window - is the expensive part. For five factors over a decade of month-ends
    this is the difference between five minutes and one.
    """
    fx_rates = fx_rates or {}
    definitions = {name: ALL_FACTORS[name] for name in factors}
    rows: dict[str, dict[dt.date, pd.Series]] = {name: {} for name in factors}

    for date in dates:
        try:
            inputs = builder.build(date, fx_rates=fx_rates)
        except ValueError:
            continue
        for name, definition in definitions.items():
            rows[name][date] = definition.compute(inputs)

    return {
        name: pd.DataFrame(data).T.sort_index() for name, data in rows.items()
    }


def build_score_panel(
    builder: FactorInputBuilder,
    dates: list[dt.date],
    factor: str,
    fx_rates: dict[str, float] | None = None,
) -> pd.DataFrame:
    """One factor's scores across many dates: a dates x securities panel.

    The input to IC analysis and Fama-MacBeth. Built date by date because every step of
    the pipeline is cross-sectional, and a vectorised version across the whole panel
    would standardise through time - which leaks the future into the past.
    """
    definition = ALL_FACTORS[factor]
    rows: dict[dt.date, pd.Series] = {}
    for date in dates:
        try:
            inputs = builder.build(date, fx_rates=fx_rates)
        except ValueError:
            continue
        rows[date] = definition.compute(inputs)
    return pd.DataFrame(rows).T.sort_index()


def month_end_dates(start: dt.date, end: dt.date, calendar: list[dt.date]) -> list[dt.date]:
    """Last trading day of each month in range."""
    by_month: dict[tuple[int, int], dt.date] = {}
    for d in calendar:
        if start <= d <= end:
            by_month[(d.year, d.month)] = d
    return sorted(by_month.values())
