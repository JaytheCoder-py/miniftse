"""Grading an extracted event by what it would do to the published index.

Classification accuracy is the wrong metric and using it would be the obvious mistake.
A misread dividend amount is nearly free. A spin-off booked as a special dividend
breaks divisor continuity and moves the level. Accuracy scores those identically.

So: apply the predicted event and the true event to the same state, and compare **both
published numbers the engine produces - the level and the divisor.** The difference is
reported in basis points. That is the number the business already uses, and it makes
the eval a risk measure rather than a proxy for one.

**Why the divisor is scored and not only the level.** `apply_event` rebases the divisor
for every `is_divisor_event` so that the level is continuous *by construction*: for a
return of capital, a rights issue, a share-count change or an ineligible spin-off, the
level after the event is identical to the level before it and is completely independent
of the event's parameters. A grader that diffs levels alone therefore reports 0.00 bps
for a 1-for-4 rights issue predicted as 3-for-1, and 0.00 bps for a split predicted as a
doubling of the share count - two events whose divisors differ by 394 and 3,333 bps
respectively. The divisor is what the methodology actually decides (spec §1.3: "it is
graded on whether the resulting divisor is right"), so it is graded, and `error_bps` is
the worse of the two errors. See D-029.

**What is still not graded, and cannot be.** A split's ratio. `Split` is
market-value-invariant by construction and is not a divisor event, so `Split(ratio=2.0)`
and `Split(ratio=10.0)` leave *identical* levels and *identical* divisors - the index
impact of a wrong split ratio genuinely is zero on the day, and reporting anything else
would be inventing a number the engine does not produce. `identical_events` is the
honest signal there: 0.00 bps with `identical_events=False` means "graded zero", not
"correct". Getting the split's *class* wrong is a different matter and is caught, which
is the failure that actually moves a published level.

**The bps figures are weight-dependent; the ratio between them is not.** The canonical
worked example below costs 67.114 bps on a three-name fixture, 204.082 bps on a
one-name index and 2.000 bps at a realistic 1% large-cap weight. Quoting 67bp
unqualified would be quoting a property of the fixture. What survives the weight is the
*ratio*: on this state a return of capital booked as an ordinary dividend costs exactly
**20x** what misreading the same dividend's amount by 5% costs, at every constituent
count, because both errors carry the same `1/(1 - a·S/MV)` denominator and it cancels.
That ratio is the claim the eval rests on. See D-022 and `test_the_misclassification_
penalty_is_weight_dependent_but_its_ratio_is_not`.

This module computes no index arithmetic of its own. It calls the engine twice and
compares what came back - a difference of two levels the engine published and a ratio of
two divisors the engine returned. Neither recomputes a level, a market value or a
divisor, which keeps one source of truth for every published figure - the same
constraint `test_desk_contains_no_index_arithmetic` enforces on the desk.
"""

from __future__ import annotations

from dataclasses import dataclass

from miniftse.calc.state import IndexState
from miniftse.corpactions.engine import CorporateActionEngine, CorporateActionError
from miniftse.corpactions.events import CorporateAction


@dataclass(frozen=True, slots=True)
class ImpactError:
    """What getting this event wrong would have cost the published level."""

    predicted_level: float
    truth_level: float
    error_bps: float
    """The headline: the worse of `level_error_bps` and `divisor_error_bps`.

    Worse, not sum: they are two views of one error, and a divisor event moves the
    divisor precisely so that the level does *not* move. Adding them would double-count
    the misclassifications where both are non-zero."""

    predicted_divisor: float
    truth_divisor: float
    same_type: bool
    """Diagnostic only. A correct type with a wrong parameter and an incorrect type
    are different failures and are reported separately."""

    level_error_bps: float
    """Difference in the published index level. Zero whenever both events are divisor
    events, since the rebase holds the level constant on both sides."""

    divisor_error_bps: float
    """`|predicted_divisor / truth_divisor - 1|`. This is the component that sees a
    misclassification between two structural events, where the level cannot."""

    identical_events: bool
    """Whether the two events are the same event, field for field.

    The guard against reading a zero as a pass. A market-value-invariant event pair
    (two splits with different ratios) scores 0.00 bps *correctly* - there is no index
    impact - but is not a correct extraction, and only this field distinguishes the two."""


@dataclass(frozen=True, slots=True)
class Ungraded:
    """The pair could not be graded, which is not the same as scoring zero.

    Deliberately not an `ImpactError` with `error_bps=0.0` and deliberately not a
    subclass of one: it has no `error_bps` attribute at all, so a caller that sums or
    percentiles this into a scoreboard gets an `AttributeError` on the spot rather than
    a run of flattering zeros. mypy narrows the union at every call site in `src/` for
    the same reason.
    """

    reason: str
    detail: str = ""


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
) -> ImpactError | Ungraded:
    """Basis points of index impact between a predicted event and the true one.

    A fresh engine per application: the engine accumulates an audit trail, and grading
    must not leave one behind.

    Returns `Ungraded` rather than raising, for three reasons, all of them the same
    reason: one bad row must not destroy a grading run over a whole corpus.

    * **The security is not in the index.** `apply_event` returns the state untouched
      with a "skipped: not a constituent" note for a security it does not hold, so both
      sides come back with the starting level and the pair scores 0.00 bps - a perfect
      mark for an ungraded comparison. This is not hypothetical: `harvest_labels` takes
      its `security_id` from vendor data ("AAPL"), and grading those against a fixture
      state would produce an all-zeros scoreboard indistinguishable from a flawless
      model. See D-030.
    * **The engine rejects an event.** `Split(ratio=0.0)` divides by zero,
      `RightsIssue(per_held=0)` and `Spinoff(shares_per_parent_share=0.0)` raise
      `ValueError`, and a wrong-typed field that slipped past construction raises
      `TypeError` inside the handler. `taxonomy.build_event` now range-checks the first
      three, but a prediction can reach here without passing through `build_event` at
      all, and the failure mode of an uncaught raise here is losing the whole batch.
      See D-032.
    * **The level or divisor is zero on the truth side**, so a relative error has no
      denominator to be expressed against.
    """
    for role, event in (("truth", truth), ("predicted", predicted)):
        if event.security_id not in state.constituents:
            return Ungraded(
                "security is not a constituent",
                f"{role} event {event.event_id!r} is on {event.security_id!r}, which is "
                f"not in this state; the engine would skip it and both sides would "
                f"report the starting level",
            )

    try:
        predicted_level, predicted_divisor = _level_after(predicted, state, withholding_tax)
        truth_level, truth_divisor = _level_after(truth, state, withholding_tax)
    except (ArithmeticError, TypeError, ValueError, CorporateActionError) as exc:
        return Ungraded("engine rejected the event", f"{type(exc).__name__}: {exc}")

    if truth_level == 0.0:
        return Ungraded("truth level is zero", "cannot express an error in basis points")
    if truth_divisor == 0.0:
        return Ungraded("truth divisor is zero", "cannot express an error in basis points")

    level_error_bps = abs(predicted_level - truth_level) / abs(truth_level) * 10_000.0
    divisor_error_bps = abs(predicted_divisor / truth_divisor - 1.0) * 10_000.0

    return ImpactError(
        predicted_level=predicted_level,
        truth_level=truth_level,
        error_bps=max(level_error_bps, divisor_error_bps),
        predicted_divisor=predicted_divisor,
        truth_divisor=truth_divisor,
        same_type=predicted.event_type is truth.event_type,
        level_error_bps=level_error_bps,
        divisor_error_bps=divisor_error_bps,
        identical_events=predicted == truth,
    )
