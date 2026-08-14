"""The label space, pinned.

`EventType` has 16 values; `CorporateActionEngine.apply_event` dispatches on 10 concrete
classes. Several types share a class and are distinguished by a field - a special
dividend is a `CashDividend` with `is_special=True`, a reverse split is a `Split` with
`ratio < 1`. A model cannot be graded against a label space that is not written down,
so this module writes it down and `test_covers_every_event_type` keeps it total.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Final, cast, get_type_hints

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

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

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

    positive: tuple[str, ...] = ()
    """Required fields that must be strictly greater than zero.

    Type-correct is not the same as constructible. `Split(ratio=0.0)` is a perfectly
    well-typed `float` and builds without complaint, then raises `ZeroDivisionError`
    inside `Split.price_effect` the moment the engine touches it; `RightsIssue`'s
    entitlement and `Spinoff.shares_per_parent_share` raise `ValueError` the same way.
    A negative ratio is worse than either - it constructs, applies, passes the engine's
    market-value-invariance check (price and shares both flip sign, so their product is
    unchanged) and leaves a negative price in the index. These are ratios and per-share
    entitlements, where the positivity is a fact about the event class rather than a
    judgement about the data, so the taxonomy can settle it once for every caller.
    See D-032."""

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
    EventType.SPLIT: EventSpec(Split, ("ratio",), {}, 1, positive=("ratio",)),
    EventType.REVERSE_SPLIT: EventSpec(
        Split,
        ("ratio",),
        {},
        1,
        positive=("ratio",),
        note="Same class; Split.event_type returns REVERSE_SPLIT when ratio < 1.",
    ),
    EventType.BONUS_ISSUE: EventSpec(
        Split,
        ("ratio",),
        {},
        3,
        positive=("ratio",),
        note="Arithmetically identical to a split - see the Split docstring.",
    ),
    EventType.RIGHTS_ISSUE: EventSpec(
        RightsIssue,
        ("subscription_price", "new_shares", "per_held", "cum_price"),
        {},
        3,
        positive=("new_shares", "per_held"),
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
        positive=("shares_per_parent_share",),
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


@functools.cache
def _field_type_hints(handler: type[CorporateAction]) -> dict[str, Any]:
    """Resolved (not stringified) field annotations for a handler dataclass.

    `corpactions/events.py` has `from __future__ import annotations`, so every
    annotation there is stored as a string under PEP 563 -
    `dataclasses.fields(handler)[i].type` is the literal string `"float"`, not the
    type object. `get_type_hints` is what actually evaluates a deferred annotation
    back into a real class, resolving it against the declaring module's globals.
    Cached per handler - the taxonomy is fixed at import time, so the resolved hints
    never change, and `build_event` runs once per announcement in a corpus batch.
    """
    return get_type_hints(handler)


@functools.cache
def _declared_fields(handler: type[CorporateAction]) -> frozenset[str]:
    """The names the handler dataclass's constructor will actually accept.

    `CorporateAction` is an ABC, not itself a dataclass, so a `type[CorporateAction]`
    does not statically satisfy `fields()`'s `DataclassInstance` protocol even though
    every concrete handler in `TAXONOMY` is `@dataclass`-decorated and satisfies it at
    runtime - the same cast `corpus.Announcement.to_dict` makes for the same reason.
    Cached per handler: the taxonomy is fixed at import time.
    """
    return frozenset(f.name for f in fields(cast("type[DataclassInstance]", handler)))


def _payload_value_matches(value: Any, expected: Any) -> bool:
    """True when `value`'s runtime type satisfies the dataclass field type `expected`.

    Two deliberate departures from a bare `isinstance` check, both chosen for what a
    model's JSON payload can actually contain rather than for type-theoretic
    completeness:

    * `int` is accepted wherever `float` is annotated. `json.loads('{"amount": 2}')`
      produces a Python `int` for a whole-number literal, and a model asked for the
      dollar amount of a $2 dividend has no reason to write "2.0" instead of "2".
    * `bool` is never accepted for a numeric field, even though `bool` is an `int`
      subclass in Python and would otherwise pass an `isinstance(value, (int,
      float))` check silently. `CashDividend(amount=True)` constructs cleanly as a
      $1.00 dividend if that is allowed through - exactly the "partially built event
      that grades cleanly and is wrong" this module's docstring names as the worst
      outcome, and worse than useless as a defence against it if the type check lets
      it straight through.
    """
    if expected is bool:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if expected is float:
        return isinstance(value, (int, float))
    if isinstance(expected, type):
        return isinstance(value, expected)
    return True


def _check_required_types(
    event_type: EventType,
    handler: type[CorporateAction],
    required: tuple[str, ...],
    payload: dict[str, Any],
) -> None:
    """Raise `TaxonomyError` on a `spec.required` value of the wrong type.

    Key presence alone (the `missing` check above) is not enough: a dataclass field
    accepts any value at construction, so `{"amount": "two dollars"}` builds a
    `CashDividend` with a string `amount` and `abstained=False` - it does not raise
    anywhere, it just hands a poisoned event downstream to detonate later inside the
    engine. This is property 3 of the module docstring made real: the taxonomy, not
    the caller, decides constructibility, so the check belongs here rather than only
    in `extract.py`, where it would protect one caller instead of every one.
    """
    hints = _field_type_hints(handler)
    for name in required:
        expected = hints.get(name)
        if expected is None:
            continue
        value = payload[name]
        if not _payload_value_matches(value, expected):
            expected_name = expected.__name__ if isinstance(expected, type) else str(expected)
            raise TaxonomyError(
                f"{event_type} field {name!r} expects {expected_name}, got "
                f"{type(value).__name__}: {value!r}"
            )


def _check_positive(
    event_type: EventType,
    positive: tuple[str, ...],
    payload: dict[str, Any],
) -> None:
    """Raise `TaxonomyError` on a ratio or entitlement that is not strictly positive.

    Type validation stops `{"ratio": "two"}`; it does not stop `{"ratio": 0.0}`, which
    is a valid `float` and builds a `Split` that raises `ZeroDivisionError` the moment
    the engine divides by it - detonating inside `apply_event`, one module away from
    the caller that could have declined. `spec.positive` lists the fields where zero
    or negative is not a datum but a defect, and the same argument that put the type
    check here rather than in `extract.py` puts the range check here: the taxonomy
    decides constructibility for every caller, not just for the one holding a model.
    """
    for name in positive:
        value = payload[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue  # already rejected by _check_required_types for a required field
        if value <= 0:
            raise TaxonomyError(f"{event_type} field {name!r} must be positive, got {value!r}")


def build_event(
    event_type: EventType,
    common: dict[str, Any],
    payload: dict[str, Any],
) -> CorporateAction:
    """Construct a `CorporateAction` from a type and a flat payload.

    `payload` must supply every field in `spec.required`, and may additionally supply
    any other field the handler dataclass declares **except a `COMMON_FIELDS` one** - a
    round-tripped corpus label passes every field it was built with, not just the
    required ones, which is what lets `Spinoff.spinco_enters_index` or
    `Delisting.final_price` survive a write/read cycle instead of silently reverting to
    a default. `spec.defaults` is applied last and always wins over `payload`: those
    fields are type-discriminating (`is_special` for a special dividend) rather than
    data, and the type itself - not whatever a caller happened to pass - decides them.

    **`common` always wins over `payload` for the five `COMMON_FIELDS`.** Identity is
    the caller's to state, not the payload's to propose: `extract_event` passes the
    `security_id` it asked the model about and the `event_id` of the row being graded,
    and a payload that could overwrite either would move the whole comparison onto a
    different constituent - grading a plausible non-zero number against the wrong name's
    index weight, or returning `Ungraded` and deleting a wrong extraction from the
    scoreboard entirely. Both branches score the model for something other than what it
    did. `corpus.to_dict` has always excluded these five from the payload it writes
    (`corpus.py:93-97`); this is the same rule enforced on the way in. See D-033.

    Keys the handler dataclass does not declare are **dropped, not fatal.** A payload
    is a description of an event written by something else - a vendor feed, a model -
    and both routinely carry fields this taxonomy has no use for: the repo's own
    synthetic universe emits `gross_amount` on every dividend and `terp` on every
    rights issue, neither of which is a constructor argument, and forwarding them
    verbatim made `spec.handler(**kwargs)` raise `TypeError` on 98% of the corpus.
    Dropping an undeclared key loses nothing the taxonomy models, whereas rejecting
    the row loses the label entirely; the fields that actually matter are the ones in
    `spec.required`, and those are checked, present, typed and ranged below. See D-031.

    Each `spec.required` value is checked against the handler dataclass's own
    annotation (`_check_required_types`) and, where the field is a ratio or an
    entitlement, against `spec.positive` (`_check_positive`) before construction - a
    dataclass field accepts any value of any type or magnitude, so key presence alone
    does not stop a wrong-typed or zero value from building an event that looks valid
    and detonates inside the engine.

    Raises rather than guessing. A silently-defaulted amount produces an event that
    applies cleanly and grades wrong, which is the worst possible failure here.
    """
    spec = TAXONOMY[event_type]
    if spec.handler is None:
        raise TaxonomyError(f"no handler for {event_type}: {spec.note}")

    if not isinstance(payload, dict):
        # `payload` is annotated `dict[str, Any]`, which checks nothing at runtime, and
        # the caller nearest a live model hands over `parsed.get("payload", {})` - a
        # JSON list or string reaches here just as easily as an object. Rejecting it
        # here keeps the filtering below from raising `AttributeError`, which no caller
        # catches, instead of the `TaxonomyError` every caller already handles.
        raise TaxonomyError(f"{event_type} payload must be an object, got {type(payload).__name__}")

    missing_common = [f for f in COMMON_FIELDS if f not in common]
    if missing_common:
        raise TaxonomyError(f"missing common fields: {', '.join(missing_common)}")

    declared = _declared_fields(spec.handler)
    payload = {k: v for k, v in payload.items() if k in declared and k not in COMMON_FIELDS}

    missing = [f for f in spec.required if f not in payload]
    if missing:
        raise TaxonomyError(f"{event_type} requires {', '.join(missing)}")

    _check_required_types(event_type, spec.handler, spec.required, payload)
    _check_positive(event_type, spec.positive, payload)

    kwargs: dict[str, Any] = dict(common)
    kwargs.update(payload)
    kwargs.update(spec.defaults)
    return spec.handler(**kwargs)
