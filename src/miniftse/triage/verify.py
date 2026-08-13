"""Grading an extracted event by what it would do to the published index.

Classification accuracy is the wrong metric and using it would be the obvious mistake.
A misread dividend amount is nearly free. A spin-off booked as a special dividend
breaks divisor continuity and moves the level. Accuracy scores those identically.

So: apply the predicted event and the true event to the same state, and report the
difference between the resulting index levels in basis points. That is the number the
business already uses, and it makes the eval a risk measure rather than a proxy for one.

This module computes no index arithmetic of its own. It calls the engine twice and
subtracts, which keeps one source of truth for every published figure - the same
constraint `test_desk_contains_no_index_arithmetic` enforces on the desk.
"""

from __future__ import annotations

from dataclasses import dataclass

from miniftse.calc.state import IndexState
from miniftse.corpactions.engine import CorporateActionEngine
from miniftse.corpactions.events import CorporateAction


@dataclass(frozen=True, slots=True)
class ImpactError:
    """What getting this event wrong would have cost the published level."""

    predicted_level: float
    truth_level: float
    error_bps: float
    predicted_divisor: float
    truth_divisor: float
    same_type: bool
    """Diagnostic only. A correct type with a wrong parameter and an incorrect type
    are different failures and are reported separately."""


def _level_after(
    event: CorporateAction,
    state: IndexState,
    withholding_tax: dict[str, float] | None,
) -> tuple[float, float]:
    engine = CorporateActionEngine(withholding_tax=dict(withholding_tax or {}))
    result = engine.apply_event(event, state)
    return result.state.level, result.state.divisor


def impact_error(
    predicted: CorporateAction,
    truth: CorporateAction,
    state: IndexState,
    *,
    withholding_tax: dict[str, float] | None = None,
) -> ImpactError:
    """Basis points of index level between a predicted event and the true one.

    A fresh engine per application: the engine accumulates an audit trail, and grading
    must not leave one behind.
    """
    predicted_level, predicted_divisor = _level_after(predicted, state, withholding_tax)
    truth_level, truth_divisor = _level_after(truth, state, withholding_tax)

    if truth_level == 0.0:
        raise ValueError("truth level is zero; cannot express an error in basis points")

    error_bps = abs(predicted_level - truth_level) / abs(truth_level) * 10_000.0

    return ImpactError(
        predicted_level=predicted_level,
        truth_level=truth_level,
        error_bps=error_bps,
        predicted_divisor=predicted_divisor,
        truth_divisor=truth_divisor,
        same_type=predicted.event_type is truth.event_type,
    )
