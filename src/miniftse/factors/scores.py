"""Point-in-time factor scores for the reconstitution engine.

`ReconstitutionEngine` accepts a `score_provider`; this is the concrete one. Without it
the factor index cannot be built at all, which makes this the join between the research
layer and the index engine.

The design constraint that shapes everything here: **scores must be computed at the
review cut-off, never at the effective date.** The engine asks for a score during
`run_review`, which happens as the calculator walks forward through time, so the provider
must resolve the correct as-of date itself rather than trusting the caller. Getting this
wrong gives the index two to five weeks of foresight at every review, which is more than
enough to manufacture a spectacular and entirely fictitious backtest.

Scores are cached per cut-off date. A review touches every candidate security, and
rebuilding the cross-section once per security would make a ten-year build take hours.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from miniftse.factors.build import FactorInputBuilder
from miniftse.factors.definitions import ALL_FACTORS, FactorDefinition, compute_all


@dataclass
class FactorScoreProvider:
    """Supplies point-in-time factor scores to the reconstitution engine.

    Duck-typed against what `ReconstitutionEngine` calls: `score(security_id, as_of)`
    and a `tilt_strength` attribute. Kept structural rather than nominal so a test can
    substitute a fixed-score stub without importing the factor machinery.
    """

    builder: FactorInputBuilder
    factor: str = "value"
    tilt_strength: float = 1.0
    """Coefficient in `w ∝ w_cap × exp(strength × score)`.

    The single most important product parameter in a tilt index. Higher means more factor
    exposure and more turnover; the turnover study in `turnover_budget_sweep` finds the
    knee."""

    fx_rates: dict[str, float] = field(default_factory=dict)
    composite_weights: dict[str, float] | None = None
    """When set, blends several factors instead of using one. Keys are factor names."""

    combination: str = "integrated"
    _cache: dict[dt.date, pd.Series] = field(default_factory=dict, repr=False)
    _requested: list[dt.date] = field(default_factory=list, repr=False)

    def score(self, security_id: str, as_of: dt.date) -> float:
        """One security's score. Zero when unavailable.

        Zero is the neutral value after standardisation, so a security with no score
        gets the cap-weighted position it would have had in the parent index. That is
        the conservative choice: the alternative - excluding it - would tilt the index
        towards whatever kinds of company happen to have complete data, which is a real
        and undocumented bias.
        """
        panel = self.scores_at(as_of)
        value = panel.get(security_id, 0.0)
        return float(value) if np.isfinite(value) else 0.0

    def scores_at(self, as_of: dt.date) -> pd.Series:
        """The whole cross-section, cached."""
        if as_of not in self._cache:
            self._cache[as_of] = self._compute(as_of)
            self._requested.append(as_of)
        return self._cache[as_of]

    def _compute(self, as_of: dt.date) -> pd.Series:
        try:
            inputs = self.builder.build(as_of, fx_rates=self.fx_rates)
        except (ValueError, KeyError):
            return pd.Series(dtype=float)

        if self.composite_weights:
            from miniftse.factors.pipeline import FactorPipeline, combine_scores

            scores = {
                name: ALL_FACTORS[name].compute(inputs)
                for name in self.composite_weights
            }
            combined = combine_scores(scores, self.composite_weights, self.combination)
            return FactorPipeline().transform(
                combined, industry=inputs.industry, country=inputs.country,
                market_cap=inputs.market_cap,
            )

        return ALL_FACTORS[self.factor].compute(inputs)

    def exposure_of(self, weights: dict[str, float], as_of: dt.date) -> float:
        """Weighted-average score of a weight vector. The index's factor exposure."""
        panel = self.scores_at(as_of)
        return float(sum(w * float(panel.get(k, 0.0)) for k, w in weights.items()))

    def active_exposure(
        self, weights: dict[str, float], benchmark: dict[str, float], as_of: dt.date
    ) -> float:
        return self.exposure_of(weights, as_of) - self.exposure_of(benchmark, as_of)

    def cache_stats(self) -> dict[str, int]:
        return {
            "cross_sections_computed": len(self._cache),
            "securities_scored": sum(len(s) for s in self._cache.values()),
        }


@dataclass
class BufferedScoreProvider:
    """Wraps a score provider with a turnover-reduction buffer on the score itself.

    The mechanism: only update a security's score if it has moved by more than
    `threshold` since the last review. Small score changes produce small weight changes
    produce trading that costs more than the exposure it buys.

    This is the score-level analogue of the size-band buffer, and it has the same
    trade-off in the same direction: less turnover, less faithful exposure. It is a
    genuine alternative to simply lowering the tilt strength, and it behaves differently
    - a buffer suppresses *churn* while preserving peak exposure, whereas a lower tilt
    reduces exposure uniformly.
    """

    inner: FactorScoreProvider
    threshold: float = 0.20
    """Score units, i.e. standard deviations of the cross-section."""

    _held: dict[str, float] = field(default_factory=dict, repr=False)
    _last_date: dt.date | None = field(default=None, repr=False)
    n_suppressed: int = 0
    n_updated: int = 0

    @property
    def tilt_strength(self) -> float:
        return self.inner.tilt_strength

    def score(self, security_id: str, as_of: dt.date) -> float:
        if self._last_date != as_of:
            self._last_date = as_of
            self.n_suppressed = 0
            self.n_updated = 0

        fresh = self.inner.score(security_id, as_of)
        held = self._held.get(security_id)
        if held is None or abs(fresh - held) > self.threshold:
            self._held[security_id] = fresh
            self.n_updated += 1
            return fresh
        self.n_suppressed += 1
        return held


# --------------------------------------------------------------------------------------
# Turnover budget study
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnoverPoint:
    parameter: float
    mechanism: str
    annual_turnover: float
    mean_active_exposure: float
    exposure_per_turnover: float
    """Factor exposure bought per unit of annual turnover. The efficiency measure, and
    the one that actually decides the parameter."""

    estimated_cost_bps: float


def turnover_budget_sweep(
    build_fn: object,
    parameters: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0),
    mechanism: str = "tilt_strength",
    cost_bps_per_turnover: float = 15.0,
) -> pd.DataFrame:
    """Sweep a turnover-control parameter and report the trade-off curve.

    `build_fn(parameter) -> (annual_turnover, mean_active_exposure)`.

    The output is the chart that justifies the chosen parameter: exposure rises with
    tilt strength, turnover rises faster, and the recommendation is the knee. Quoting a
    tilt strength without this curve is asserting a number rather than choosing one.

    Cost is quoted at 15bp round-trip, which is the assumption the conclusion is most
    sensitive to and is therefore stated rather than buried.
    """
    rows: list[dict[str, float | str]] = []
    for parameter in parameters:
        turnover, exposure = build_fn(parameter)  # type: ignore[operator]
        rows.append({
            "parameter": parameter,
            "mechanism": mechanism,
            "annual_turnover": turnover,
            "mean_active_exposure": exposure,
            "exposure_per_turnover": exposure / turnover if turnover > 0 else 0.0,
            "estimated_cost_bps": turnover * cost_bps_per_turnover,
            "exposure_per_cost_bp": (
                exposure / (turnover * cost_bps_per_turnover)
                if turnover > 0 else 0.0
            ),
        })
    frame = pd.DataFrame(rows)

    # The knee: where the marginal exposure per marginal unit of turnover falls below
    # half its value at the least aggressive setting. Crude, explicit, and better than
    # eyeballing a chart - a reviewer can disagree with the rule rather than the taste.
    if len(frame) > 2:
        d_exposure = frame["mean_active_exposure"].diff()
        d_turnover = frame["annual_turnover"].diff()
        marginal = (d_exposure / d_turnover).bfill()
        frame["marginal_exposure_per_turnover"] = marginal
        first = marginal.iloc[0] if len(marginal) else 0.0
        frame["past_knee"] = marginal < 0.5 * first
    return frame


def recommend_parameter(sweep: pd.DataFrame) -> dict[str, object]:
    """Pick a parameter from the sweep, with the reasoning attached."""
    if sweep.empty:
        return {"recommendation": None, "reason": "no sweep data"}
    past = sweep[sweep.get("past_knee", pd.Series(dtype=bool)).fillna(False)]
    if past.empty:
        best = sweep.iloc[-1]
        reason = (
            "Marginal exposure per unit of turnover has not yet halved across the range "
            "tested, so the curve has no knee here. The recommendation is the most "
            "aggressive setting tested, and the range should be extended before "
            "committing."
        )
    else:
        idx = max(past.index[0] - 1, 0)
        best = sweep.loc[idx]
        reason = (
            f"At a tilt strength of {best['parameter']:g} the index achieves "
            f"{best['mean_active_exposure']:.3f} of active factor exposure for "
            f"{best['annual_turnover']:.1%} annual one-way turnover, an estimated "
            f"{best['estimated_cost_bps']:.1f}bp a year in trading cost. Beyond this "
            "point each additional unit of exposure costs more than twice as much "
            "turnover as the first, so the extra exposure stops being worth buying."
        )
    return {
        "recommendation": float(best["parameter"]),
        "annual_turnover": float(best["annual_turnover"]),
        "active_exposure": float(best["mean_active_exposure"]),
        "estimated_cost_bps": float(best["estimated_cost_bps"]),
        "reason": reason,
    }


def factor_definition(name: str) -> FactorDefinition:
    return ALL_FACTORS[name]


def score_summary(provider: FactorScoreProvider, as_of: dt.date) -> pd.DataFrame:
    """Diagnostics for one review's scores. Read before every launch.

    A factor whose scores are not roughly mean-zero unit-variance has failed
    standardisation somewhere, and the tilt strength then means something different from
    what the methodology document says it means.
    """
    panel = provider.scores_at(as_of)
    if panel.empty:
        return pd.DataFrame()
    return pd.DataFrame([{
        "as_of": as_of,
        "n_scored": int(panel.notna().sum()),
        "mean": float(panel.mean()),
        "std": float(panel.std(ddof=1)),
        "min": float(panel.min()),
        "p10": float(panel.quantile(0.10)),
        "median": float(panel.median()),
        "p90": float(panel.quantile(0.90)),
        "max": float(panel.max()),
        "n_zero": int((panel == 0.0).sum()),
    }])


def all_factor_scores(builder: FactorInputBuilder, as_of: dt.date,
                      fx_rates: dict[str, float] | None = None) -> pd.DataFrame:
    """Every factor's scores for one cross-section, for the correlation diagnostics."""
    inputs = builder.build(as_of, fx_rates=fx_rates or {})
    return compute_all(inputs)
