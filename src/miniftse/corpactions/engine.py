"""Applying corporate actions to index state.

One function does the real work::

    apply_event(event, state) -> EventResult

and its shape is the same for every event type:

1. Record market value **before**.
2. Apply the price and share effects to the affected constituent(s).
3. If the event is structural, rebase the divisor so the level is continuous.
   If it is a market move, leave the divisor alone and let the level move.
4. Record what happened, so the change is auditable years later.

Step 3 is the entire methodology. Everything else is bookkeeping.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace

from miniftse.calc.state import Constituent, DivisorChange, IndexState
from miniftse.corpactions.events import (
    CashDividend,
    CashMerger,
    CorporateAction,
    Delisting,
    EventType,
    FloatChange,
    ReturnOfCapital,
    RightsIssue,
    SharesChange,
    Spinoff,
    Split,
    StockMerger,
)
from miniftse.types import EPSILON


class CorporateActionError(RuntimeError):
    """An event cannot be applied to this state."""


@dataclass(frozen=True, slots=True)
class _Handled:
    """What a handler gives back to the dispatcher."""

    state: IndexState
    cash: float = 0.0
    net_cash: float = 0.0
    created: tuple[Constituent, ...] = ()
    notes: tuple[str, ...] = ()
    mv_before_override: float | None = None
    """The market value the divisor rebase must preserve, when it is not the value at
    the start of the event. Used by removals, which recognise a final price move before
    the constituent leaves."""


@dataclass(frozen=True, slots=True)
class EventResult:
    """The outcome of applying one event."""

    state: IndexState
    change: DivisorChange
    cash_distributed: float = 0.0
    """Base-currency cash distributed to the index, gross of withholding. Drives the
    total return calculation."""

    net_cash_distributed: float = 0.0
    """After withholding tax, for the net total return index."""

    new_constituents: tuple[Constituent, ...] = ()
    """Securities the event created - a spin-off child entering the index."""

    notes: tuple[str, ...] = ()


@dataclass
class CorporateActionEngine:
    """Applies events, keeping an audit trail of every divisor movement."""

    withholding_tax: dict[str, float] = field(default_factory=dict)
    """Rate by issuer country. Absent means zero, which is the right default only
    because the reference universe is fully populated - in production a missing rate
    should be a blocking validation error, not a silent zero."""

    spinco_eligibility: dict[str, bool] = field(default_factory=dict)
    """Whether each spinco is eligible for the index. Populated by the review engine
    from the eligibility screens; defaults to True."""

    audit: list[DivisorChange] = field(default_factory=list)

    # ------------------------------------------------------------------ dispatch

    def apply_event(self, event: CorporateAction, state: IndexState) -> EventResult:
        """Apply one event and return the new state plus an audit record."""
        constituent = state.constituents.get(event.security_id)
        if constituent is None and not isinstance(event, StockMerger):
            # Events fire for securities outside the index all the time. Not an error;
            # there is simply nothing to do.
            return EventResult(
                state=state,
                change=self._null_change(event, state, "security is not a constituent"),
                notes=("skipped: not a constituent",),
            )

        mv_before = state.total_market_value
        level_before = state.level if state.divisor else 0.0
        divisor_before = state.divisor

        handler = {
            CashDividend: self._apply_distribution,
            ReturnOfCapital: self._apply_distribution,
            Split: self._apply_split,
            RightsIssue: self._apply_rights,
            Spinoff: self._apply_spinoff,
            CashMerger: self._apply_cash_merger,
            StockMerger: self._apply_stock_merger,
            Delisting: self._apply_delisting,
            SharesChange: self._apply_shares_change,
            FloatChange: self._apply_float_change,
        }.get(type(event))

        if handler is None:
            raise CorporateActionError(f"no handler for {type(event).__name__}")

        handled: _Handled = handler(event, state)  # type: ignore[operator]
        new_state = handled.state

        if event.is_divisor_event:
            # Removals recognise a final price move *before* the constituent leaves, so
            # the market value the rebase must preserve is the one that includes that
            # move - not the one from the start of the day. Handlers that need this say
            # so with `mv_before_override`; rebasing against the wrong baseline silently
            # erases the takeover premium or the delisting loss from the index return.
            baseline = handled.mv_before_override
            new_state = new_state.rebase_divisor(baseline if baseline is not None else mv_before)
            reason = f"{event.event_type}: structural change, divisor rebased"
        else:
            reason = f"{event.event_type}: market move, divisor unchanged"

        change = DivisorChange(
            date=event.ex_date,
            event_id=event.event_id,
            event_type=str(event.event_type),
            security_id=event.security_id,
            divisor_before=divisor_before,
            divisor_after=new_state.divisor,
            market_value_before=mv_before,
            market_value_after=new_state.total_market_value,
            level_before=level_before,
            level_after=new_state.level if new_state.divisor else 0.0,
            reason=reason,
            market_value_at_rebase=(
                handled.mv_before_override if handled.mv_before_override is not None else mv_before
            ),
        )
        self.audit.append(change)
        return EventResult(
            state=new_state,
            change=change,
            cash_distributed=handled.cash,
            net_cash_distributed=handled.net_cash,
            new_constituents=handled.created,
            notes=handled.notes,
        )

    def apply_all(
        self, events: list[CorporateAction], state: IndexState
    ) -> tuple[IndexState, float, float, list[EventResult]]:
        """Apply a day's events in order, accumulating distributed cash.

        Handlers return states that are already complete - a spin-off child is inserted
        by the spin-off handler, not bolted on here - so that the audit record for each
        event describes the state the event actually produced.
        """
        gross = net = 0.0
        results: list[EventResult] = []
        for event in events:
            result = self.apply_event(event, state)
            state = result.state
            gross += result.cash_distributed
            net += result.net_cash_distributed
            results.append(result)
        return state, gross, net, results

    # ------------------------------------------------------------------ handlers

    def _apply_distribution(
        self, event: CashDividend | ReturnOfCapital, state: IndexState
    ) -> _Handled:
        """Cash dividend or return of capital.

        Identical arithmetic, opposite divisor treatment - and that difference is the
        entire reason the two classes exist. The dividend's price drop is a market move
        the price index is supposed to show; the capital return's is not.
        """
        c = state.constituents[event.security_id]
        amount = event.amount
        new_price = max(c.price - amount, 0.0)
        new_state = state.replace_constituent(c.with_price(new_price))

        # Shares held by the index, in base currency: the cash the index receives.
        held = c.shares * c.free_float_factor * c.capping_factor
        gross_cash = amount * held * c.fx_rate

        rate = self.withholding_tax.get(str(c.country), 0.0)
        net_cash = gross_cash * (1.0 - rate)

        note = (
            f"withholding {rate:.2%} applied for {c.country}"
            if rate
            else f"no withholding tax for {c.country}"
        )
        return _Handled(new_state, cash=gross_cash, net_cash=net_cash, notes=(note,))

    def _apply_split(self, event: Split, state: IndexState) -> _Handled:
        """Price down by the ratio, shares up by the ratio.

        Market value is invariant, so the divisor must not move. The assertion below is
        deliberate: a split that changes market value means the ratio was applied to
        only one of the two, which is the single most common index arithmetic bug.
        """
        c = state.constituents[event.security_id]
        mv_before = c.market_value
        new_c = replace(
            c,
            price=event.price_effect(c.price).apply(c.price),
            shares=event.share_effect(c.shares).apply(c.shares),
        )
        drift = abs(new_c.market_value - mv_before) / max(mv_before, EPSILON)
        if drift > 1e-9:
            raise CorporateActionError(
                f"split on {event.security_id} changed market value by {drift:.2e} - "
                "price and share effects are inconsistent"
            )
        return _Handled(
            state.replace_constituent(new_c),
            notes=(f"ratio {event.ratio}, market value invariant",),
        )

    def _apply_rights(self, event: RightsIssue, state: IndexState) -> _Handled:
        """Price falls to TERP, share count rises, new capital enters.

        The index gains market value equal to the cash subscribed, but the holder paid
        that cash, so the level must not rise. The divisor absorbs it.
        """
        c = state.constituents[event.security_id]
        new_c = replace(
            c,
            price=event.terp,
            shares=event.share_effect(c.shares).apply(c.shares),
        )
        note = (
            f"TERP {event.terp:.4f} from cum {event.cum_price:.4f}, "
            f"{event.new_shares}-for-{event.per_held} at {event.subscription_price:.4f}; "
            f"rights value {event.rights_value:.4f}/share"
        )
        return _Handled(state.replace_constituent(new_c), notes=(note,))

    def _apply_spinoff(self, event: Spinoff, state: IndexState) -> _Handled:
        """Parent price falls by the distributed value; spinco may or may not enter."""
        c = state.constituents[event.security_id]
        new_price = max(c.price - event.value_per_parent_share, 0.0)
        new_state = state.replace_constituent(c.with_price(new_price))

        eligible = self.spinco_eligibility.get(event.spinco_security_id, event.spinco_enters_index)
        if eligible:
            # Spinco enters at the implied value, inserted here rather than by the
            # caller so the audit record sees the completed state.
            #
            # It inherits the parent's capping factor. That looks like a detail and is
            # not: the parent's market value fell by `value * shares * float * cap`, so
            # the spinco must arrive with the same cap factor for total market value to
            # be preserved. Give it 1.0 instead and a capped parent leaves a hole in the
            # index exactly the size of its capping discount. Capping is recomputed at
            # the next review anyway.
            spinco = Constituent(
                security_id=event.spinco_security_id,
                price=event.spinco_price,
                shares=c.shares * event.shares_per_parent_share,
                free_float_factor=c.free_float_factor,
                capping_factor=c.capping_factor,
                fx_rate=c.fx_rate,
                currency=c.currency,
                country=c.country,
                icb_industry=c.icb_industry,
                size_band=c.size_band,
                adv=c.adv,
            )
            note = (
                f"spinco {event.spinco_security_id} enters at {event.spinco_price:.4f}; "
                "total market value preserved, divisor unchanged"
            )
            return _Handled(new_state.replace_constituent(spinco), created=(spinco,), notes=(note,))

        # Spinco is ineligible: the distributed value leaves the index. Treated as a
        # distribution for total return, because the holder did receive it.
        held = c.shares * c.free_float_factor * c.capping_factor
        cash = event.value_per_parent_share * held * c.fx_rate
        rate = self.withholding_tax.get(str(c.country), 0.0)
        note = (
            f"spinco {event.spinco_security_id} is ineligible; "
            f"{event.value_per_parent_share:.4f}/share leaves the index and the "
            "divisor is rebased"
        )
        return _Handled(new_state, cash=cash, net_cash=cash * (1 - rate), notes=(note,))

    def _apply_cash_merger(self, event: CashMerger, state: IndexState) -> _Handled:
        """Constituent leaves at the deal price.

        The final move from the last close to the deal price is a genuine return and
        must be recognised *before* removal. Deleting at the previous close instead
        erases the takeover premium, which flatters the index.
        """
        c = state.constituents[event.security_id]
        at_deal = c.with_price(event.cash_per_share)
        with_final_move = state.replace_constituent(at_deal)
        # The level after recognising the deal price is what continuity must preserve,
        # so it is handed to the dispatcher as the rebase baseline rather than rebased
        # here - doing both would apply the adjustment twice.
        mv_with_move = with_final_move.total_market_value
        removed = with_final_move.remove_constituent(event.security_id)

        note = (
            f"removed at cash consideration {event.cash_per_share:.4f}; final move "
            "recognised before deletion"
        )
        return _Handled(removed, notes=(note,), mv_before_override=mv_with_move)

    def _apply_stock_merger(self, event: StockMerger, state: IndexState) -> _Handled:
        """Target leaves; acquirer's share count rises by the exchange ratio.

        If the acquirer is not a constituent the target's value simply exits - the
        methodology could instead fast-track the acquirer in, which is a legitimate
        alternative and is noted rather than silently chosen.
        """
        target = state.constituents.get(event.security_id)
        if target is None:
            return _Handled(state, notes=("target is not a constituent",))

        at_deal = target.with_price(event.implied_value_per_share)
        working = state.replace_constituent(at_deal)
        mv_with_move = working.total_market_value

        acquirer = working.constituents.get(event.acquirer_security_id)
        working = working.remove_constituent(event.security_id)

        if acquirer is not None:
            issued = event.new_acquirer_shares(target.shares)
            working = working.replace_constituent(
                replace(acquirer, shares=acquirer.shares + issued)
            )
            note = (
                f"target absorbed into {event.acquirer_security_id}; "
                f"{issued:,.0f} shares issued at ratio {event.exchange_ratio}"
            )
        else:
            note = (
                f"acquirer {event.acquirer_security_id} is not a constituent; target "
                "value exits the index. A fast-entry rule could instead admit the "
                "acquirer here - a published methodology choice."
            )

        return _Handled(working, notes=(note,), mv_before_override=mv_with_move)

    def _apply_delisting(self, event: Delisting, state: IndexState) -> _Handled:
        """Removal at the final price, recognising the loss first."""
        c = state.constituents[event.security_id]
        at_final = c.with_price(event.final_price)
        working = state.replace_constituent(at_final)
        mv_with_loss = working.total_market_value
        removed = working.remove_constituent(event.security_id)
        note = (
            f"delisted ({event.reason}) at {event.final_price:.4f}; loss recognised "
            "before removal so the index does not silently drop it"
        )
        return _Handled(removed, notes=(note,), mv_before_override=mv_with_loss)

    def _apply_shares_change(self, event: SharesChange, state: IndexState) -> _Handled:
        c = state.constituents[event.security_id]
        new_c = replace(c, shares=event.new_shares)
        note = (
            f"shares {event.old_shares:,.0f} -> {event.new_shares:,.0f} ({event.pct_change:+.2%})"
        )
        return _Handled(state.replace_constituent(new_c), notes=(note,))

    def _apply_float_change(self, event: FloatChange, state: IndexState) -> _Handled:
        c = state.constituents[event.security_id]
        new_c = replace(c, free_float_factor=event.new_float)
        note = f"free float {event.old_float:.4f} -> {event.new_float:.4f}"
        return _Handled(state.replace_constituent(new_c), notes=(note,))

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _null_change(event: CorporateAction, state: IndexState, reason: str) -> DivisorChange:
        level = state.level if state.divisor else 0.0
        return DivisorChange(
            date=event.ex_date,
            event_id=event.event_id,
            event_type=str(event.event_type),
            security_id=event.security_id,
            divisor_before=state.divisor,
            divisor_after=state.divisor,
            market_value_before=state.total_market_value,
            market_value_after=state.total_market_value,
            level_before=level,
            level_after=level,
            reason=reason,
        )

    def audit_frame(self):  # type: ignore[no-untyped-def]
        """The divisor audit trail as a DataFrame, for the daily production report."""
        import pandas as pd

        return pd.DataFrame(
            [
                {
                    "date": c.date,
                    "event_id": c.event_id,
                    "event_type": c.event_type,
                    "security_id": c.security_id,
                    "divisor_before": c.divisor_before,
                    "divisor_after": c.divisor_after,
                    "divisor_change_pct": c.divisor_change_pct,
                    "level_before": c.level_before,
                    "level_after": c.level_after,
                    "continuity_error_bps": c.level_continuity_error_bps,
                    "realised_return_bps": c.realised_return_bps,
                    "reason": c.reason,
                }
                for c in self.audit
            ]
        )

    def continuity_breaches(self, tolerance_bps: float = 1.0) -> list[DivisorChange]:
        """Divisor events where the level moved when it should not have.

        Should always be empty. If it is not, either an event was misclassified or the
        rebase was applied against the wrong 'before' market value - and either way the
        published level is wrong.
        """
        return [
            c
            for c in self.audit
            if c.event_type
            not in {
                EventType.CASH_DIVIDEND,
                EventType.SPECIAL_DIVIDEND,
                EventType.SPLIT,
                EventType.REVERSE_SPLIT,
            }
            and abs(c.level_continuity_error_bps) > tolerance_bps
        ]


def divisor_adjustment(
    divisor: float, market_value_before: float, market_value_after: float
) -> float:
    """The formula on its own, for teaching and for hand-checking.

    D_new = D_old * MV_after / MV_before
    """
    if market_value_before <= EPSILON:
        raise ValueError("market value before must be positive")
    return divisor * market_value_after / market_value_before


def events_on(events: list[CorporateAction], date: dt.date) -> list[CorporateAction]:
    return [e for e in events if e.ex_date == date]
