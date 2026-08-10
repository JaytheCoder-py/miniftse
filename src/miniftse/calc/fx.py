"""Currency conversion and index-level hedging.

Two separate jobs that get conflated:

* **Conversion** turns local prices into the index base currency. Every multi-currency
  index does this and it is not optional.
* **Hedging** is a product decision. A hedged index overlays short forward positions in
  each foreign currency, rolled on a schedule, sized to the index weights at the reset.

The thing worth understanding is why a hedged index return is *not* the local return.
Between resets the hedge notional is fixed while the underlying value moves, so the
hedge is imperfect by construction. The residual is hedge error, and decomposing
realised hedged return into local return + carry + hedge error is the analysis a client
asks for the first time a hedged index misses its unhedged sibling by more than they
expected.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


class FxError(RuntimeError):
    """A rate is missing, and guessing one is not acceptable in a published index."""


@dataclass
class FxTable:
    """Spot rates and deposit rates, indexed for fast lookup.

    Rates are quoted as units of base currency per one unit of quote currency, so
    ``local_value * rate`` converts into base. Getting the direction backwards is a
    classic incident - it is silent, it affects only foreign names, and it looks like a
    factor return.
    """

    base: str
    spot: dict[tuple[dt.date, str], float] = field(default_factory=dict)
    deposit: dict[tuple[dt.date, str], float] = field(default_factory=dict)
    _dates: list[dt.date] = field(default_factory=list, repr=False)

    @classmethod
    def from_frame(cls, fx: pd.DataFrame, deposits: pd.DataFrame | None = None,
                   base: str = "USD") -> FxTable:
        table = cls(base=base)
        for row in fx.itertuples(index=False):
            table.spot[(_as_date(row.date), str(row.quote))] = float(row.rate)
        if deposits is not None:
            for row in deposits.itertuples(index=False):
                table.deposit[(_as_date(row.date), str(row.currency))] = float(
                    row.deposit_rate)
        table._dates = sorted({d for d, _ in table.spot})
        return table

    def rate(self, date: dt.date, currency: str) -> float:
        if currency == self.base:
            return 1.0
        hit = self.spot.get((date, currency))
        if hit is not None:
            return hit
        # Fall back to the most recent earlier rate. A weekend or a local holiday is
        # legitimate; a gap of weeks is a data defect, so it is bounded and reported.
        prior = [d for d in self._dates if d <= date]
        if not prior:
            raise FxError(f"no {currency}/{self.base} rate on or before {date}")
        last = prior[-1]
        if (date - last).days > 7:
            raise FxError(
                f"{currency}/{self.base} is stale: nearest rate is {last}, "
                f"{(date - last).days} days before {date}"
            )
        value = self.spot.get((last, currency))
        if value is None:
            raise FxError(f"no {currency}/{self.base} rate on or before {date}")
        return value

    def deposit_rate(self, date: dt.date, currency: str) -> float:
        hit = self.deposit.get((date, currency))
        if hit is not None:
            return hit
        prior = [d for d in self._dates if d <= date]
        for d in reversed(prior):
            v = self.deposit.get((d, currency))
            if v is not None:
                return v
        raise FxError(f"no deposit rate for {currency} on or before {date}")

    def currencies(self) -> set[str]:
        return {c for _, c in self.spot}


def forward_rate(
    spot: float, base_rate: float, quote_rate: float, tenor_years: float
) -> float:
    """Covered interest parity.

        F = S * (1 + r_base * t) / (1 + r_quote * t)

    Used to synthesise a forward curve from deposit rates when quoted forwards are not
    available. The relationship holds by arbitrage in liquid currency pairs, so the
    synthesised forward is close enough for index construction - and where it is not,
    the deviation (cross-currency basis) is itself a tradeable spread and a sign the
    approximation has stopped being safe.
    """
    return spot * (1.0 + base_rate * tenor_years) / (1.0 + quote_rate * tenor_years)


def forward_points(spot: float, forward: float) -> float:
    return forward - spot


def carry(base_rate: float, quote_rate: float, tenor_years: float) -> float:
    """Interest rate differential earned by being short the forward.

    Positive when the base currency yields more than the quote currency - which is the
    entire economics of a currency hedge, and the reason a GBP-hedged USD index has not
    returned the same as the unhedged one even before hedge error.
    """
    return (base_rate - quote_rate) * tenor_years


@dataclass(frozen=True, slots=True)
class HedgeLeg:
    currency: str
    notional_local: float
    forward_rate: float
    spot_at_open: float
    open_date: dt.date
    maturity: dt.date

    def mark_to_market(self, spot_now: float) -> float:
        """Value of the short-forward position in base currency.

        Short the foreign currency forward: profit when it weakens against base, so the
        sign is (contracted rate - current spot) times notional.
        """
        return self.notional_local * (self.forward_rate - spot_now)


@dataclass
class HedgedIndexCalculator:
    """Monthly-reset currency hedge over a multi-currency index.

    Mechanics, which are the same at every provider give or take the reset schedule:

    1. On each reset, sell forward the index's exposure to each foreign currency,
       one month out, at the CIP forward rate.
    2. Each day, mark the forwards to spot. That P&L is added to the unhedged return.
    3. At the next reset, close and re-strike at the new exposures.

    Hedge error arises because step 1 fixes the notional while the underlying value
    moves through the month. If foreign assets rise, the hedge is too small and the
    index keeps some currency exposure; if they fall, it is over-hedged. The error is
    second-order in normal months and very much first-order in a crisis, which is
    exactly when someone asks about it.
    """

    fx: FxTable
    hedge_ratio: float = 1.0
    reset_day: int = 1
    tenor_months: int = 1

    _legs: dict[str, HedgeLeg] = field(default_factory=dict, repr=False)
    _last_reset: dt.date | None = field(default=None, repr=False)

    def should_reset(self, date: dt.date) -> bool:
        if self._last_reset is None:
            return True
        return (date.year, date.month) != (self._last_reset.year, self._last_reset.month)

    def reset(self, date: dt.date, exposures_local: dict[str, float]) -> None:
        """Re-strike the hedge against the current currency exposures.

        `exposures_local` is the index market value attributable to each currency,
        expressed in that currency.
        """
        tenor = self.tenor_months / 12.0
        self._legs = {}
        for ccy, notional in exposures_local.items():
            if ccy == self.fx.base or notional == 0:
                continue
            spot = self.fx.rate(date, ccy)
            fwd = forward_rate(
                spot,
                self.fx.deposit_rate(date, self.fx.base),
                self.fx.deposit_rate(date, ccy),
                tenor,
            )
            self._legs[ccy] = HedgeLeg(
                currency=ccy,
                notional_local=notional * self.hedge_ratio,
                forward_rate=fwd,
                spot_at_open=spot,
                open_date=date,
                maturity=date + dt.timedelta(days=int(30.44 * self.tenor_months)),
            )
        self._last_reset = date

    def mark(self, date: dt.date) -> float:
        """Total hedge P&L in base currency, marked to today's spot."""
        return sum(
            leg.mark_to_market(self.fx.rate(date, ccy)) for ccy, leg in self._legs.items()
        )

    def decompose(
        self,
        date: dt.date,
        exposures_local: dict[str, float],
    ) -> dict[str, float]:
        """Split the hedge position into carry and hedge error.

        * **carry** - the forward premium locked in at the reset, the part that was
          knowable in advance.
        * **hedge error** - the mismatch between the hedged notional and the actual
          exposure now, valued at the currency move since the reset. This is the part
          nobody can forecast and everybody asks about.
        """
        carry_total = 0.0
        error_total = 0.0
        for ccy, leg in self._legs.items():
            spot_now = self.fx.rate(date, ccy)
            elapsed = max((date - leg.open_date).days, 0) / 365.25
            tenor = self.tenor_months / 12.0
            fwd_premium = (leg.forward_rate - leg.spot_at_open) * leg.notional_local
            carry_total += fwd_premium * (elapsed / tenor if tenor else 0.0)

            actual = exposures_local.get(ccy, 0.0) * self.hedge_ratio
            mismatch = actual - leg.notional_local
            error_total += -mismatch * (spot_now - leg.spot_at_open)

        return {
            "carry": carry_total,
            "hedge_error": error_total,
            "total_hedge_pnl": self.mark(date),
        }


def convert_series(
    values: pd.Series, currencies: pd.Series, dates: pd.Series, fx: FxTable
) -> pd.Series:
    """Vectorised local-to-base conversion with an explicit missing-rate failure."""
    rates = np.array([
        fx.rate(_as_date(d), str(c)) for d, c in zip(dates, currencies)
    ])
    return values * rates


def currency_exposures(
    market_values_base: dict[str, float], currencies: dict[str, str], fx: FxTable,
    date: dt.date,
) -> dict[str, float]:
    """Index market value by currency, expressed in each local currency.

    The input to a hedge reset. Note the division: exposures must be in local terms to
    size a forward, and converting back is where a sign or direction error hides.
    """
    out: dict[str, float] = {}
    for sec, mv_base in market_values_base.items():
        ccy = currencies.get(sec, fx.base)
        rate = fx.rate(date, ccy)
        if rate == 0:
            raise FxError(f"zero {ccy} rate on {date}")
        out[ccy] = out.get(ccy, 0.0) + mv_base / rate
    return out


def _as_date(value: object) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.Timestamp(value).date()  # type: ignore[arg-type]
