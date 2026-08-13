"""The label space, pinned.

`EventType` has 16 values; `CorporateActionEngine.apply_event` dispatches on 10 concrete
classes. Several types share a class and are distinguished by a field - a special
dividend is a `CashDividend` with `is_special=True`, a reverse split is a `Split` with
`ratio < 1`. A model cannot be graded against a label space that is not written down,
so this module writes it down and `test_covers_every_event_type` keeps it total.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

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

COMMON_FIELDS: Final[tuple[str, ...]] = (
    "event_id",
    "security_id",
    "ex_date",
    "announcement_date",
    "pay_date",
)


@dataclass(frozen=True, slots=True)
class EventSpec:
    """One row of the label space."""

    handler: type[CorporateAction] | None
    required: tuple[str, ...]
    """Fields beyond COMMON_FIELDS that the handler needs."""

    defaults: dict[str, Any]
    """Fields the handler needs that are implied by the event type rather than read
    from the announcement - `is_special=True` for a special dividend."""

    in_scope_stage: int | None
    """Stage at which this type enters the eval set. None means unmapped."""

    note: str = ""


TAXONOMY: Final[dict[EventType, EventSpec]] = {
    EventType.CASH_DIVIDEND: EventSpec(CashDividend, ("amount",), {"is_special": False}, 1),
    EventType.SPECIAL_DIVIDEND: EventSpec(
        CashDividend,
        ("amount",),
        {"is_special": True},
        3,
        note="Same class as CASH_DIVIDEND. The line between them is a published "
        "materiality threshold, which is exactly why it is a stage-3 class.",
    ),
    EventType.RETURN_OF_CAPITAL: EventSpec(
        ReturnOfCapital,
        ("amount",),
        {},
        3,
        note="Identical price effect to a cash dividend, opposite divisor treatment. "
        "The canonical misclassification.",
    ),
    EventType.SPLIT: EventSpec(Split, ("ratio",), {}, 1),
    EventType.REVERSE_SPLIT: EventSpec(
        Split,
        ("ratio",),
        {},
        1,
        note="Same class; Split.event_type returns REVERSE_SPLIT when ratio < 1.",
    ),
    EventType.BONUS_ISSUE: EventSpec(
        Split,
        ("ratio",),
        {},
        3,
        note="Arithmetically identical to a split - see the Split docstring.",
    ),
    EventType.RIGHTS_ISSUE: EventSpec(
        RightsIssue,
        ("subscription_price", "new_shares", "per_held", "cum_price"),
        {},
        3,
        note="TERP needs the full terms - a rights issue cannot be summarised as one "
        "ratio. `new_shares`/`per_held` is the entitlement (2-for-5 is 2 and 5).",
    ),
    EventType.SPINOFF: EventSpec(
        Spinoff,
        (
            "spinco_security_id",
            "shares_per_parent_share",
            "value_per_parent_share",
            "parent_cum_price",
        ),
        {},
        3,
    ),
    EventType.MERGER_CASH: EventSpec(
        CashMerger,
        ("cash_per_share",),
        {},
        3,
        note="`cash_per_share` shadows the base class property of the same name; see "
        "the type: ignore on the dataclass field.",
    ),
    EventType.MERGER_STOCK: EventSpec(
        StockMerger,
        ("acquirer_security_id", "exchange_ratio", "implied_value_per_share"),
        {},
        3,
    ),
    EventType.DELISTING: EventSpec(
        Delisting,
        (),
        {},
        3,
        note="`final_price` and `reason` both default. `final_price` defaulting to 0.0 "
        "means a delisting extracted without one writes the position to zero - "
        "consider promoting it to required before stage 3 admits this class.",
    ),
    EventType.SHARES_CHANGE: EventSpec(SharesChange, ("new_shares", "old_shares"), {}, 3),
    EventType.FLOAT_CHANGE: EventSpec(FloatChange, ("new_float", "old_float"), {}, 3),
    EventType.STOCK_DIVIDEND: EventSpec(
        None,
        (),
        {},
        None,
        note="Declared in EventType (events.py:34) but no dataclass reports it from "
        "`event_type`, `parse_event` has no case for it (falls through to the "
        "`unhandled event type` branch), and CorporateActionEngine.apply_event's "
        "dispatch dict (engine.py:112-123) has no handler keyed for it. Confirmed "
        'via `grep -rn "STOCK_DIVIDEND" src/miniftse/` - the only hit outside '
        "this file is the enum member itself. No concrete handler exists.",
    ),
    EventType.TENDER_OFFER: EventSpec(
        None,
        (),
        {},
        None,
        note="Declared in EventType (events.py:43) but no dataclass reports it from "
        "`event_type`, `parse_event` has no case for it, and the engine's "
        "dispatch dict (engine.py:112-123) has no handler keyed for it. Confirmed "
        'via `grep -rn "TENDER_OFFER" src/miniftse/` - the only hit outside this '
        "file is the enum member itself. No concrete handler exists.",
    ),
    EventType.SUSPENSION: EventSpec(
        None,
        (),
        {},
        None,
        note="Declared in EventType (events.py:47) but no dataclass reports it from "
        "`event_type`, `parse_event` has no case for it, and the engine's "
        "dispatch dict (engine.py:112-123) has no handler keyed for it. Confirmed "
        'via `grep -rn "SUSPENSION" src/miniftse/` - the only hit outside this '
        "file is the enum member itself. No concrete handler exists.",
    ),
}


class TaxonomyError(ValueError):
    """The requested event cannot be constructed."""


def build_event(
    event_type: EventType,
    common: dict[str, Any],
    payload: dict[str, Any],
) -> CorporateAction:
    """Construct a `CorporateAction` from a type and a flat payload.

    Raises rather than guessing. A silently-defaulted amount produces an event that
    applies cleanly and grades wrong, which is the worst possible failure here.
    """
    spec = TAXONOMY[event_type]
    if spec.handler is None:
        raise TaxonomyError(f"no handler for {event_type}: {spec.note}")

    missing_common = [f for f in COMMON_FIELDS if f not in common]
    if missing_common:
        raise TaxonomyError(f"missing common fields: {', '.join(missing_common)}")

    missing = [f for f in spec.required if f not in payload]
    if missing:
        raise TaxonomyError(f"{event_type} requires {', '.join(missing)}")

    kwargs: dict[str, Any] = dict(common)
    kwargs.update({f: payload[f] for f in spec.required})
    kwargs.update(spec.defaults)
    return spec.handler(**kwargs)
