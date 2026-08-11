"""Size banding with buffer zones.

Bands are cumulative-percentile cuts through the investable universe sorted by
free-float market cap: the largest names summing to 70% of total value are Large, the
next 15% Mid, and so on.

The interesting part is the buffer. A hard cut-off means a company sitting on the
Mid/Small line moves band whenever it wobbles, and each move forces every fund tracking
either index to trade. The buffer makes membership sticky: an incumbent must cross the
boundary by a margin before it is moved.

The cost is honest and should be stated to clients rather than buried: with buffers the
index no longer means exactly "the top 70% by value". Two names of identical size can
sit in different bands depending on where they came from - path dependence, deliberately
accepted, because the turnover saving is worth more than the definitional purity.
`buffer_study` quantifies that trade-off.

**Determinism.** Band membership is a rule, and a rule whose outcome depends on
floating-point summation order is not a rule. This module therefore refuses to let the
arithmetic decide anything the ground rules have not: the universe is put in a *total*
order (`_ordered`), the cumulative percentile is summed *exactly* (`_exact_cumulative`),
and every comparison against a cutoff or a buffer edge happens on values quantised to
`CUMULATIVE_PCT_DECIMALS` decimal places with a written-down tie-break (Ground Rules
2.1 and 8.3). See DECISIONS.md D-014 for the incident that forced this.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from miniftse.config import BandingConfig
from miniftse.types import SizeBand

CUMULATIVE_PCT_DECIMALS = 12
"""Decimal places the cumulative percentile is rounded to before any band comparison.

Chosen deliberately, and the same number appears in Ground Rules 2.1 and DECISIONS.md
D-014 - three places, because a precision that lives only in code is not a rule anyone
can hold us to.

Twelve, because: `_exact_cumulative` is correctly rounded, so its relative error is at
most half an ulp - around 1e-16 on a cumulative share of order 1. Twelve decimal places
sits four orders of magnitude above that noise floor, so quantisation never masks a real
difference; and 1e-12 of cumulative index weight is 1e-8 of a basis point, which is
immaterial by every threshold in this repository (the golden master's tolerance is
0.1bp, i.e. 1e-5 of weight - seven orders of magnitude coarser).

What it buys: "exactly on the cutoff" becomes a *decidable* state with a documented
outcome, rather than a coin-flip settled by whichever last bit the platform's summation
happened to produce.
"""


@dataclass(frozen=True, slots=True)
class BandAssignment:
    security_id: str
    band: SizeBand
    cumulative_pct: float
    float_market_cap: float
    previous_band: SizeBand | None
    moved: bool
    held_by_buffer: bool
    """True when a hard cut-off would have moved this name but the buffer kept it.
    Counted at every review: it is the buffer's output, and the number to quote when
    someone asks what the buffer is doing."""

    def explain(self) -> str:
        if self.held_by_buffer:
            return (
                f"{self.security_id} sits at the {self.cumulative_pct:.1%} cumulative "
                f"mark, which is inside the {self.previous_band} band's buffer, so it "
                f"stays in {self.band} rather than moving. This is the buffer rule "
                "working as designed: it suppresses turnover from names oscillating "
                "around a boundary."
            )
        if self.moved:
            return (
                f"{self.security_id} moved from {self.previous_band} to {self.band}; "
                f"it is now at the {self.cumulative_pct:.1%} cumulative mark, far "
                "enough past the boundary to clear the buffer."
            )
        return f"{self.security_id} remains {self.band} at {self.cumulative_pct:.1%}."


def assign_bands(
    float_market_caps: dict[str, float],
    config: BandingConfig,
    previous_bands: dict[str, SizeBand] | None = None,
) -> dict[str, BandAssignment]:
    """Assign every security to a size band, applying buffers to incumbents.

    Sorted largest first, cumulative share of total float market cap computed, and the
    boundaries applied. With `apply_buffers`, an incumbent keeps its band unless it has
    crossed the relevant boundary by more than `buffer_width`.

    Bit-for-bit reproducible across platforms and numpy builds: see `_ordered` (total
    order), `_exact_cumulative` (exact summation) and `_quantise` (documented precision
    before every comparison). The `cumulative_pct` reported on each `BandAssignment` is
    the quantised value the rule was actually decided on, not a rawer number that would
    let a diagnostic disagree with the assignment it is explaining.
    """
    if not float_market_caps:
        return {}
    previous_bands = previous_bands or {}

    ordered = _ordered(float_market_caps)
    total = math.fsum(v for _, v in ordered)
    if total <= 0:
        raise ValueError("total float market cap must be positive")

    cumulative = [_quantise(c / total) for c in _exact_cumulative([v for _, v in ordered])]
    boundaries = [
        (config.large_cutoff, SizeBand.LARGE),
        (config.mid_cutoff, SizeBand.MID),
        (config.small_cutoff, SizeBand.SMALL),
    ]

    out: dict[str, BandAssignment] = {}
    for (sec_id, cap), cum in zip(ordered, cumulative, strict=True):
        hard = _band_for(cum, boundaries)
        prev = previous_bands.get(sec_id)

        band, held = hard, False
        if (
            config.apply_buffers
            and prev is not None
            and prev != hard
            and _within_buffer(cum, prev, hard, boundaries, config.buffer_width)
        ):
            band, held = prev, True

        out[sec_id] = BandAssignment(
            security_id=sec_id, band=band, cumulative_pct=cum,
            float_market_cap=cap, previous_band=prev,
            moved=prev is not None and band != prev, held_by_buffer=held,
        )
    return out


def _ordered(float_market_caps: dict[str, float]) -> list[tuple[str, float]]:
    """The universe in a *total* order: largest float market cap first, ties broken by
    ascending `security_id`.

    A plain `sorted(..., key=itemgetter(1), reverse=True)` is only a partial order. It
    is stable, so two securities of exactly equal size come out in whatever order the
    caller's dict happened to iterate - which makes the band of a name on a boundary a
    function of upstream insertion order rather than of anything in the ground rules.
    Ordering ties by security id is arbitrary but *written down* (Ground Rules 2.1),
    which is the property that matters: the same universe always produces the same
    ranking, whoever assembled the dict and in whatever order.
    """
    return sorted(float_market_caps.items(), key=lambda kv: (-kv[1], kv[0]))


def _exact_cumulative(values: Sequence[float]) -> list[float]:
    """Running sum of `values` with every prefix correctly rounded, in one pass.

    `np.cumsum` was here, and it is the wrong tool for a rule. numpy sums in blocks
    whose size and order depend on the SIMD width and the BLAS build, so the last bits
    of the cumulative percentile differ between platforms - which is exactly how a
    security sitting on a band cutoff landed in Large on one machine and Mid on another
    (DECISIONS.md D-014).

    This is Shewchuk's exact partial-sum algorithm, the same one `math.fsum` uses
    internally, kept open so the running total can be read after every element instead
    of only at the end. `[math.fsum(values[:i + 1]) for i in range(len(values))]` gives
    bit-identical output (asserted in the tests) but costs O(n^2); this is O(n).

    Because every prefix is the correctly rounded value of the *exact* mathematical
    sum, the result depends only on the multiset of values and their order - never on
    how the machine chose to associate the additions.
    """
    partials: list[float] = []
    out: list[float] = []
    for value in values:
        x = value
        index = 0
        for partial in partials:
            y = partial
            if abs(x) < abs(y):
                x, y = y, x
            hi = x + y
            lo = y - (hi - x)
            if lo:
                partials[index] = lo
                index += 1
            x = hi
        del partials[index:]
        partials.append(x)
        out.append(math.fsum(partials))
    return out


def _quantise(value: float) -> float:
    """Round to `CUMULATIVE_PCT_DECIMALS` places - the only form of a cumulative
    percentile that is ever compared against a cutoff or a buffer edge."""
    return round(value, CUMULATIVE_PCT_DECIMALS)


def _band_for(cum: float, boundaries: list[tuple[float, SizeBand]]) -> SizeBand:
    """The hard-cut-off band for a cumulative percentile.

    **Tie-break (Ground Rules 2.1).** Both sides are quantised to
    `CUMULATIVE_PCT_DECIMALS` first, and the comparison is inclusive: a security whose
    quantised cumulative percentile is *exactly* a cutoff belongs to the band that
    cutoff closes - the larger band. That is what "the top 70% by value" means in
    English, and it is now what it means in code, on every platform.

    The bare `<=` against an unquantised `np.cumsum` output that used to be here made
    that boundary case a coin-flip settled by the last bit of a summation. `cum` is
    already quantised by `assign_bands`; it is quantised again here so the rule holds
    for any other caller too, and `_quantise` of an already-quantised value is a no-op.
    """
    q_cum = _quantise(cum)
    for cutoff, band in boundaries:
        if q_cum <= _quantise(cutoff):
            return band
    return SizeBand.MICRO


def _within_buffer(
    cum: float,
    previous: SizeBand,
    hard: SizeBand,
    boundaries: list[tuple[float, SizeBand]],
    width: float,
) -> bool:
    """Is the security still inside the buffer around the boundary it just crossed?

    Only adjacent-band moves get buffer protection. A name that has fallen two bands in
    one review has not wobbled - something real happened - and holding it would misstate
    the index rather than stabilise it.

    **Tie-break (Ground Rules 8.3).** This comparison carried the same exposure as the
    cutoff test in `_band_for` - a bare `<=` against an unquantised distance - so it
    gets the same treatment: the distance from the boundary is quantised to
    `CUMULATIVE_PCT_DECIMALS` before being compared to the (likewise quantised) buffer
    width, and the comparison is inclusive. A security sitting at *exactly*
    `buffer_width` from the boundary is inside the buffer and is held. Ground Rules 8.3
    says a constituent moves only when it crosses "by more than" the buffer; exactly the
    buffer width is not more than it.
    """
    order = [SizeBand.LARGE, SizeBand.MID, SizeBand.SMALL, SizeBand.MICRO]
    try:
        prev_ix, hard_ix = order.index(previous), order.index(hard)
    except ValueError:
        return False
    if abs(prev_ix - hard_ix) != 1:
        return False

    boundary_ix = min(prev_ix, hard_ix)
    if boundary_ix >= len(boundaries):
        return False
    boundary = boundaries[boundary_ix][0]
    return _quantise(abs(_quantise(cum) - _quantise(boundary))) <= _quantise(width)


def band_summary(assignments: dict[str, BandAssignment]) -> pd.DataFrame:
    """Counts and weights per band, plus how many names the buffer held."""
    if not assignments:
        return pd.DataFrame()
    rows = [
        {
            "security_id": a.security_id, "band": str(a.band),
            "float_market_cap": a.float_market_cap, "cumulative_pct": a.cumulative_pct,
            "moved": a.moved, "held_by_buffer": a.held_by_buffer,
        }
        for a in assignments.values()
    ]
    df = pd.DataFrame(rows)
    total = df["float_market_cap"].sum()
    return (
        df.groupby("band", as_index=False)
        .agg(n=("security_id", "size"),
             market_cap=("float_market_cap", "sum"),
             n_moved=("moved", "sum"),
             n_held_by_buffer=("held_by_buffer", "sum"))
        .assign(weight=lambda d: d["market_cap"] / total)
        .sort_values("market_cap", ascending=False)
        .reset_index(drop=True)
    )


# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BufferStudyResult:
    buffer_width: float
    annual_turnover: float
    n_band_moves: int
    n_round_trips: int
    """Names that left a band and came back within a year. The pure waste a buffer is
    there to eliminate: every round trip is two sets of trades that achieved nothing."""

    mean_band_purity: float
    """Share of names whose band matches what a hard cut-off would have given. Falls as
    the buffer widens - the definitional cost."""


def buffer_study(
    cap_history: list[dict[str, float]],
    config: BandingConfig,
    widths: tuple[float, ...] = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08),
) -> pd.DataFrame:
    """Sweep buffer width over a history of reviews and report the trade-off.

    Produces the chart that justifies the chosen width: turnover falls steeply at first
    and then flattens, while band purity declines roughly linearly. The recommendation
    is the knee, and being able to point at it is what turns "we use a 2% buffer" from
    an assertion into a decision.
    """
    from dataclasses import replace as dc_replace

    rows: list[dict[str, float | int]] = []
    for width in widths:
        cfg = dc_replace(config, buffer_width=width, apply_buffers=width > 0)
        previous: dict[str, SizeBand] = {}
        band_history: list[dict[str, SizeBand]] = []
        moves = 0
        purity_samples: list[float] = []

        for caps in cap_history:
            assignments = assign_bands(caps, cfg, previous)
            hard = assign_bands(caps, dc_replace(cfg, apply_buffers=False), None)
            moves += sum(1 for a in assignments.values() if a.moved)
            purity_samples.append(
                np.mean([assignments[k].band == hard[k].band for k in assignments])
                if assignments else 1.0
            )
            previous = {k: a.band for k, a in assignments.items()}
            band_history.append(previous)

        turnover = _band_turnover(band_history, cap_history)
        rows.append({
            "buffer_width": width,
            "annual_turnover": turnover,
            "n_band_moves": moves,
            "n_round_trips": _count_round_trips(band_history),
            "mean_band_purity": float(np.mean(purity_samples)),
        })
    return pd.DataFrame(rows)


def _band_turnover(
    band_history: list[dict[str, SizeBand]], cap_history: list[dict[str, float]]
) -> float:
    """Weight moving between bands per review, annualised at four reviews a year."""
    if len(band_history) < 2:
        return 0.0
    total = 0.0
    for (prev, curr), caps in zip(zip(band_history, band_history[1:], strict=False),
        cap_history[1:], strict=False):
        denom = sum(caps.values()) or 1.0
        moved = sum(
            caps.get(k, 0.0) for k in curr
            if k in prev and prev[k] != curr[k]
        )
        total += moved / denom
    return total / (len(band_history) - 1) * 4


def _count_round_trips(band_history: list[dict[str, SizeBand]], window: int = 4) -> int:
    """Names that left a band and returned within `window` reviews."""
    count = 0
    all_names = {k for snapshot in band_history for k in snapshot}
    for name in all_names:
        path = [snap.get(name) for snap in band_history]
        for i in range(len(path) - 2):
            if path[i] is None or path[i + 1] is None:
                continue
            if path[i] == path[i + 1]:
                continue
            horizon = path[i + 2: i + 2 + window]
            if path[i] in horizon:
                count += 1
                break
    return count
