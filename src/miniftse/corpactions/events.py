"""Corporate action event model.

One class per event type, each carrying exactly the fields needed to compute its effect
on price, share count and the index divisor. The taxonomy matters more than the code:
getting an event into the right class is most of the work, and the classic incidents are
misclassifications - a return of capital booked as an ordinary dividend, a scheme of
arrangement booked as a delisting.

Each event answers three questions, and the engine does nothing but ask them:

1. What happens to the **price**?
2. What happens to the **share count**?
3. Does the change in index market value represent a **market move** (divisor unchanged)
   or a **structural change** (divisor absorbs it)?

Question 3 is the one that decides whether your published index level jumps.
"""

from __future__ import annotations

import datetime as dt
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pandas as pd


class EventType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    SPECIAL_DIVIDEND = "SPECIAL_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    RETURN_OF_CAPITAL = "RETURN_OF_CAPITAL"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    BONUS_ISSUE = "BONUS_ISSUE"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    SPINOFF = "SPINOFF"
    MERGER_CASH = "MERGER_CASH"
    MERGER_STOCK = "MERGER_STOCK"
    TENDER_OFFER = "TENDER_OFFER"
    SHARES_CHANGE = "SHARES_CHANGE"
    FLOAT_CHANGE = "FLOAT_CHANGE"
    DELISTING = "DELISTING"
    SUSPENSION = "SUSPENSION"


@dataclass(frozen=True, slots=True)
class PriceEffect:
    """How an event changes the quoted price on the ex-date.

    `multiplier` handles proportional changes (splits), `subtract` handles per-share
    distributions (dividends, spin-offs). Applied as ``p * multiplier - subtract``, in
    that order, so a split-and-dividend on the same date composes correctly.
    """

    multiplier: float = 1.0
    subtract: float = 0.0

    def apply(self, price: float) -> float:
        return price * self.multiplier - self.subtract


@dataclass(frozen=True, slots=True)
class ShareEffect:
    multiplier: float = 1.0
    absolute: float | None = None

    def apply(self, shares: float) -> float:
        return self.absolute if self.absolute is not None else shares * self.multiplier


class CorporateAction(ABC):
    """Base event. Subclasses declare their own effects."""

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date

    @property
    @abstractmethod
    def event_type(self) -> EventType: ...

    @abstractmethod
    def price_effect(self, price: float) -> PriceEffect: ...

    @abstractmethod
    def share_effect(self, shares: float) -> ShareEffect: ...

    @property
    def is_divisor_event(self) -> bool:
        """True when the change in index market value is structural rather than a
        market move, so the divisor must absorb it to keep the level continuous."""
        return True

    @property
    def cash_per_share(self) -> float:
        """Cash distributed to holders on the ex-date. Feeds the total return
        calculation; zero for events that distribute nothing."""
        return 0.0

    @property
    def removes_constituent(self) -> bool:
        return False

    def describe(self) -> str:
        return f"{self.event_type} on {self.security_id} ex {self.ex_date}"


# --------------------------------------------------------------------------------------
# Distributions
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CashDividend(CorporateAction):
    """An ordinary cash dividend.

    The one event where the divisor does **not** change. The ex-date price drop is
    treated as a genuine market move: a price index legitimately falls by the dividend,
    and the total return index recovers it by reinvesting on the ex-date (not the pay
    date - the convention that trips people up).
    """

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date
    amount: float
    currency: str = "USD"
    is_special: bool = False
    withholding_rate: float = 0.0

    @property
    def event_type(self) -> EventType:
        return EventType.SPECIAL_DIVIDEND if self.is_special else EventType.CASH_DIVIDEND

    def price_effect(self, price: float) -> PriceEffect:
        return PriceEffect(subtract=self.amount)

    def share_effect(self, shares: float) -> ShareEffect:
        return ShareEffect()

    @property
    def is_divisor_event(self) -> bool:
        return False

    @property
    def cash_per_share(self) -> float:
        return self.amount

    @property
    def net_cash_per_share(self) -> float:
        """After withholding, for the net total return index."""
        return self.amount * (1.0 - self.withholding_rate)


@dataclass(frozen=True, slots=True)
class ReturnOfCapital(CorporateAction):
    """A capital return, and the reason `SPECIAL_DIVIDEND` is not just a big dividend.

    Most index methodologies treat a distribution above a materiality threshold - often
    a few percent of price - as a return of capital rather than income: the divisor is
    adjusted so the price index does not show a spurious loss. Below the threshold it is
    handled as ordinary income. The threshold is a published rule, and where the line
    sits is a real methodology decision.
    """

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date
    amount: float
    currency: str = "USD"

    @property
    def event_type(self) -> EventType:
        return EventType.RETURN_OF_CAPITAL

    def price_effect(self, price: float) -> PriceEffect:
        return PriceEffect(subtract=self.amount)

    def share_effect(self, shares: float) -> ShareEffect:
        return ShareEffect()

    @property
    def is_divisor_event(self) -> bool:
        return True

    @property
    def cash_per_share(self) -> float:
        return self.amount


# --------------------------------------------------------------------------------------
# Share-count events
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Split(CorporateAction):
    """Forward or reverse split, and bonus issues, which are arithmetically identical.

    Market cap is unchanged by construction, so **the divisor must not move**. Getting
    this backwards is the canonical index bug and is why `tests/test_properties.py`
    asserts divisor invariance across splits specifically.
    """

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date
    ratio: float
    """New shares per old share. 10.0 is a 10-for-1 forward split; 0.125 is 1-for-8."""

    @property
    def event_type(self) -> EventType:
        return EventType.SPLIT if self.ratio >= 1.0 else EventType.REVERSE_SPLIT

    def price_effect(self, price: float) -> PriceEffect:
        return PriceEffect(multiplier=1.0 / self.ratio)

    def share_effect(self, shares: float) -> ShareEffect:
        return ShareEffect(multiplier=self.ratio)

    @property
    def is_divisor_event(self) -> bool:
        # Price down by r, shares up by r: market value is identical, so there is
        # nothing for the divisor to absorb.
        return False


@dataclass(frozen=True, slots=True)
class SharesChange(CorporateAction):
    """A buyback, secondary offering, or scheduled share-count update.

    Index market value changes with no price move, so this is a pure divisor event.
    Whether it is implemented immediately or held to the next review is a methodology
    threshold, not a data question - see `ReviewConfig.intra_review_shares_threshold`.
    """

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date
    new_shares: float
    old_shares: float

    @property
    def event_type(self) -> EventType:
        return EventType.SHARES_CHANGE

    def price_effect(self, price: float) -> PriceEffect:
        return PriceEffect()

    def share_effect(self, shares: float) -> ShareEffect:
        return ShareEffect(absolute=self.new_shares)

    @property
    def pct_change(self) -> float:
        return (self.new_shares / self.old_shares - 1.0) if self.old_shares else 0.0


@dataclass(frozen=True, slots=True)
class FloatChange(CorporateAction):
    """A free-float revision: a lock-up expiring, a government stake sold, a founder
    selling down. No price effect, no share-count effect, but the investable market
    value moves - so the divisor absorbs it."""

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date
    new_float: float
    old_float: float

    @property
    def event_type(self) -> EventType:
        return EventType.FLOAT_CHANGE

    def price_effect(self, price: float) -> PriceEffect:
        return PriceEffect()

    def share_effect(self, shares: float) -> ShareEffect:
        return ShareEffect()


# --------------------------------------------------------------------------------------
# Rights
# --------------------------------------------------------------------------------------


def theoretical_ex_rights_price(
    cum_price: float, subscription_price: float, new_shares: int, per_held: int
) -> float:
    """TERP: the price at which the stock should open ex-rights.

        TERP = (N_held * P_cum + N_new * P_sub) / (N_held + N_new)

    It is a weighted average of what a holder already owns and what they are entitled to
    buy. The price *should* fall to it, and a holder who takes up the rights is
    unaffected - which is the whole reason TERP exists. Without it, the ex-date price
    drop looks like a loss and every naive return calculation reports one.

    Worked example, straight out of the Module 1 self-check: a 1-for-4 rights issue at a
    30% discount to a cum price of 100 has a subscription price of 70, so
    ``TERP = (4*100 + 1*70) / 5 = 94``. The price falls 6%, the holder loses nothing,
    and the index divisor rises to absorb the new capital.
    """
    if new_shares <= 0 or per_held <= 0:
        raise ValueError("rights ratio must be positive")
    return (per_held * cum_price + new_shares * subscription_price) / (per_held + new_shares)


def rights_value_per_existing_share(
    cum_price: float, subscription_price: float, new_shares: int, per_held: int
) -> float:
    """Value of the right attaching to one existing share: ``P_cum - TERP``."""
    return cum_price - theoretical_ex_rights_price(
        cum_price, subscription_price, new_shares, per_held
    )


@dataclass(frozen=True, slots=True)
class RightsIssue(CorporateAction):
    """A discounted offer of new shares to existing holders, pro rata.

    A divisor event, because new capital enters the index. The shareholder is not
    better off - they paid for the shares - so the index level must not jump, which
    means the divisor rises by exactly the ratio of new to old index market value.
    """

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date
    subscription_price: float
    new_shares: int
    per_held: int
    cum_price: float
    currency: str = "USD"

    @property
    def event_type(self) -> EventType:
        return EventType.RIGHTS_ISSUE

    @property
    def terp(self) -> float:
        return theoretical_ex_rights_price(
            self.cum_price, self.subscription_price, self.new_shares, self.per_held
        )

    @property
    def rights_value(self) -> float:
        return self.cum_price - self.terp

    def price_effect(self, price: float) -> PriceEffect:
        # Expressed as a multiplier off the cum price so the effect is scale-free and
        # still correct if the recorded cum price differs slightly from the last close.
        return PriceEffect(multiplier=self.terp / self.cum_price)

    def share_effect(self, shares: float) -> ShareEffect:
        return ShareEffect(multiplier=1.0 + self.new_shares / self.per_held)

    @property
    def cash_raised_per_old_share(self) -> float:
        return self.subscription_price * self.new_shares / self.per_held


# --------------------------------------------------------------------------------------
# Structural events
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Spinoff(CorporateAction):
    """A distribution of shares in a subsidiary.

    The interesting case, and the one that breaks naive return calculations: the
    parent's price falls with no dividend recorded anywhere. Whether the divisor moves
    depends entirely on a methodology choice:

    * **spinco enters the index** - parent market value falls, spinco market value
      arrives, total is unchanged, divisor unchanged.
    * **spinco does not enter** (ineligible, too small, wrong market) - the distributed
      value leaves the index, so the divisor falls to keep the level continuous.

    Either is defensible. Silently doing one while the Ground Rules say the other is
    what generates a recalculation event.
    """

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date
    spinco_security_id: str
    shares_per_parent_share: float
    value_per_parent_share: float
    parent_cum_price: float
    spinco_enters_index: bool = True
    currency: str = "USD"

    @property
    def event_type(self) -> EventType:
        return EventType.SPINOFF

    def price_effect(self, price: float) -> PriceEffect:
        return PriceEffect(subtract=self.value_per_parent_share)

    def share_effect(self, shares: float) -> ShareEffect:
        return ShareEffect()

    @property
    def spinco_price(self) -> float:
        """Implied opening price of the spinco, from the distributed value."""
        if self.shares_per_parent_share <= 0:
            raise ValueError("spin-off ratio must be positive")
        return self.value_per_parent_share / self.shares_per_parent_share

    @property
    def is_divisor_event(self) -> bool:
        return not self.spinco_enters_index

    @property
    def cash_per_share(self) -> float:
        """Treated as a distribution for total return purposes when the spinco is not
        held: economically the holder received value, even though no cash moved."""
        return 0.0 if self.spinco_enters_index else self.value_per_parent_share


@dataclass(frozen=True, slots=True)
class CashMerger(CorporateAction):
    """Constituent acquired for cash. It leaves the index at the deal price."""

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date
    cash_per_share: float  # type: ignore[assignment]
    currency: str = "USD"

    @property
    def event_type(self) -> EventType:
        return EventType.MERGER_CASH

    def price_effect(self, price: float) -> PriceEffect:
        return PriceEffect(multiplier=0.0)

    def share_effect(self, shares: float) -> ShareEffect:
        return ShareEffect(absolute=0.0)

    @property
    def removes_constituent(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class StockMerger(CorporateAction):
    """Constituent acquired for acquirer stock.

    Harder than a cash deal for a replicating fund: the target's weight transfers to the
    acquirer, whose share count rises. If both were already constituents the index
    concentrates; if the acquirer was not, it enters off-cycle. Both are real cases with
    published treatments.
    """

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date
    acquirer_security_id: str
    exchange_ratio: float
    implied_value_per_share: float
    currency: str = "USD"

    @property
    def event_type(self) -> EventType:
        return EventType.MERGER_STOCK

    def price_effect(self, price: float) -> PriceEffect:
        return PriceEffect(multiplier=0.0)

    def share_effect(self, shares: float) -> ShareEffect:
        return ShareEffect(absolute=0.0)

    @property
    def removes_constituent(self) -> bool:
        return True

    def new_acquirer_shares(self, target_shares: float) -> float:
        return target_shares * self.exchange_ratio


@dataclass(frozen=True, slots=True)
class Delisting(CorporateAction):
    """Removal without consideration: bankruptcy, nationalisation, a failed listing.

    The value goes to zero or to whatever the final print was. Because the loss is real,
    it must flow through the index return before the constituent is removed - deleting
    it at the previous close instead would silently erase the loss, which is the
    survivorship bias that makes an index look better than the assets it represents.
    """

    event_id: str
    security_id: str
    ex_date: dt.date
    announcement_date: dt.date
    pay_date: dt.date
    final_price: float = 0.0
    reason: str = "DELISTED"

    @property
    def event_type(self) -> EventType:
        return EventType.DELISTING

    def price_effect(self, price: float) -> PriceEffect:
        return PriceEffect(multiplier=0.0, subtract=-self.final_price)

    def share_effect(self, shares: float) -> ShareEffect:
        return ShareEffect(absolute=0.0)

    @property
    def removes_constituent(self) -> bool:
        return True


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------

_SPECIAL_DIVIDEND_ROC_THRESHOLD = 0.05
"""A special dividend above 5% of the cum price is reclassified as a return of capital
and adjusts the divisor. Published rule, arbitrary number, defended in DECISIONS.md."""


def parse_event(row: dict[str, Any], *, cum_price: float | None = None) -> CorporateAction:
    """Build a typed event from a `corp_actions` row.

    `cum_price` lets the parser apply the materiality test that separates a large
    special dividend from ordinary income. Without it, everything below the threshold
    is assumed.
    """
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)

    kind = str(row["event_type"])
    base = {
        "event_id": str(row["event_id"]),
        "security_id": str(row["security_id"]),
        "ex_date": _as_date(row["ex_date"]),
        "announcement_date": _as_date(row["announcement_date"]),
        "pay_date": _as_date(row["pay_date"]),
    }

    match kind:
        case "CASH_DIVIDEND":
            return CashDividend(
                **base,
                amount=float(payload["amount"]),
                currency=payload.get("currency", "USD"),
                is_special=False,
            )
        case "SPECIAL_DIVIDEND":
            amount = float(payload["amount"])
            if cum_price and amount / cum_price > _SPECIAL_DIVIDEND_ROC_THRESHOLD:
                return ReturnOfCapital(
                    **base, amount=amount, currency=payload.get("currency", "USD")
                )
            return CashDividend(
                **base, amount=amount, currency=payload.get("currency", "USD"), is_special=True
            )
        case "RETURN_OF_CAPITAL":
            return ReturnOfCapital(
                **base, amount=float(payload["amount"]), currency=payload.get("currency", "USD")
            )
        case "SPLIT" | "REVERSE_SPLIT" | "BONUS_ISSUE":
            return Split(**base, ratio=float(payload["ratio"]))
        case "RIGHTS_ISSUE":
            return RightsIssue(
                **base,
                subscription_price=float(payload["subscription_price"]),
                new_shares=int(payload["new_shares"]),
                per_held=int(payload["per_held"]),
                cum_price=float(payload["cum_price"]),
                currency=payload.get("currency", "USD"),
            )
        case "SPINOFF":
            return Spinoff(
                **base,
                spinco_security_id=str(payload["spinco_security_id"]),
                shares_per_parent_share=float(payload["shares_per_parent_share"]),
                value_per_parent_share=float(payload["value_per_parent_share"]),
                parent_cum_price=float(payload["parent_cum_price"]),
                spinco_enters_index=bool(payload.get("spinco_enters_index", True)),
                currency=payload.get("currency", "USD"),
            )
        case "MERGER_CASH":
            return CashMerger(
                **base,
                cash_per_share=float(payload["cash_per_share"]),
                currency=payload.get("currency", "USD"),
            )
        case "MERGER_STOCK":
            return StockMerger(
                **base,
                acquirer_security_id=str(payload["acquirer_security_id"]),
                exchange_ratio=float(payload["exchange_ratio"]),
                implied_value_per_share=float(payload["implied_value_per_share"]),
                currency=payload.get("currency", "USD"),
            )
        case "DELISTING":
            return Delisting(
                **base,
                final_price=float(payload.get("final_price", 0.0)),
                reason=str(payload.get("reason", "DELISTED")),
            )
        case "SHARES_CHANGE":
            return SharesChange(
                **base,
                new_shares=float(payload["new_shares"]),
                old_shares=float(payload["old_shares"]),
            )
        case "FLOAT_CHANGE":
            return FloatChange(
                **base, new_float=float(payload["new_float"]), old_float=float(payload["old_float"])
            )
        case _:
            raise ValueError(f"unhandled event type {kind!r}")


def parse_events(
    frame: pd.DataFrame, prices: dict[str, float] | None = None
) -> list[CorporateAction]:
    """Parse a whole `corp_actions` frame, sorted into application order."""
    if frame.empty:
        return []
    prices = prices or {}
    events = [
        parse_event(row, cum_price=prices.get(str(row["security_id"])))
        for row in frame.to_dict("records")
    ]
    return sorted(
        events, key=lambda e: (e.ex_date, _APPLY_ORDER.get(e.event_type, 50), e.security_id)
    )


_APPLY_ORDER: dict[EventType, int] = {
    # Order within a date matters when two events hit the same security. Distributions
    # are struck off the cum price first; splits then rescale; structural changes land
    # last so they see the final price and share count.
    EventType.CASH_DIVIDEND: 10,
    EventType.SPECIAL_DIVIDEND: 11,
    EventType.RETURN_OF_CAPITAL: 12,
    EventType.SPLIT: 20,
    EventType.REVERSE_SPLIT: 20,
    EventType.BONUS_ISSUE: 21,
    EventType.RIGHTS_ISSUE: 30,
    EventType.SPINOFF: 40,
    EventType.SHARES_CHANGE: 50,
    EventType.FLOAT_CHANGE: 51,
    EventType.MERGER_STOCK: 60,
    EventType.MERGER_CASH: 61,
    EventType.DELISTING: 62,
}


_APPLY_ORDER_BY_VALUE: dict[str, int] = {str(k): v for k, v in _APPLY_ORDER.items()}
"""`_APPLY_ORDER` re-keyed by plain `str`. `EventType` is a `StrEnum`, so a plain string
already looks up correctly against `_APPLY_ORDER` at runtime - but `dict.get`'s
overloads are keyed on the literal `EventType` type, so a `str` argument does not
type-check even though it behaves identically. This copy exists to give `apply_order`
a signature callers outside this module can use without importing `EventType` too."""


def apply_order(event_type: str) -> int:
    """Where an event type sits in same-date application order.

    A small public accessor over `_APPLY_ORDER`, for callers outside this module - the
    ops desk's day explanation, in particular - that want to show *why* a same-date
    dividend applies before a split without reaching into a private module attribute.
    Anything not in the table (there is no `EventType` member missing from it; this
    only matters for a caller passing something else, such as the divisor audit
    trail's synthetic ``"REVIEW"`` rows) gets the same default `parse_events` falls back
    to for an unrecognised type.
    """
    return _APPLY_ORDER_BY_VALUE.get(event_type, 50)


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.Timestamp(value).date()
