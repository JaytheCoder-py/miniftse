"""Weighters: the pluggable step between "which securities" and "how much of each".

`ReconstitutionEngine` selects a candidate set; a `Weighter` turns it into weights. The
separation is what makes index variants cheap — the parent, a factor tilt, a top-decile
selection, a capacity-constrained version and an optimised version differ only in this
object, and share every screen, buffer, calendar and corporate-action rule.

Each weighter reports `diagnostics()`, because at a review the questions are always the
same: what exposure did we get, what did it cost in turnover, and what bound.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import pandas as pd

from miniftse.weighting.schemes import (
    SecurityInputs,
    capacity_constrained_weights,
    float_market_cap_weights,
    score_tilt_weights,
    selection_weights,
    weighted_average_days_to_trade,
)


@dataclass
class WeightingContext:
    """What a weighter may know beyond the candidate set itself."""

    as_of: Any = None
    previous_weights: dict[str, float] = field(default_factory=dict)
    parent_weights: dict[str, float] = field(default_factory=dict)
    returns: pd.DataFrame | None = None
    """Trailing daily returns for the candidates, for the optimised weighter's risk
    model. Ends at the cut-off, never the effective date."""

    industry: dict[str, str] = field(default_factory=dict)
    country: dict[str, str] = field(default_factory=dict)
    fund_size: float = 0.0


class Weighter(Protocol):
    name: str

    def weights(
        self, inputs: Mapping[str, SecurityInputs], context: WeightingContext
    ) -> dict[str, float]: ...

    def diagnostics(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------------------


@dataclass
class FloatCapWeighter:
    """The parent index. Free-float market capitalisation."""

    name: str = "float_cap"
    _last: dict[str, Any] = field(default_factory=dict, repr=False)

    def weights(self, inputs: Mapping[str, SecurityInputs],
                context: WeightingContext) -> dict[str, float]:
        w = float_market_cap_weights(inputs)
        self._last = {"n_constituents": len(w), "max_weight": max(w.values(), default=0)}
        return w

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._last)


@dataclass
class TiltWeighter:
    """Cap weight scaled by a function of the factor score.

    `w ∝ w_cap × exp(strength × score)`.

    The capacity-friendly route from a signal to an index: every eligible name keeps a
    position, so turnover comes from score changes rather than from names entering and
    leaving. The price is weaker exposure than selection or optimisation would give.
    """

    strength: float = 1.0
    name: str = "tilt"
    _last: dict[str, Any] = field(default_factory=dict, repr=False)

    def weights(self, inputs: Mapping[str, SecurityInputs],
                context: WeightingContext) -> dict[str, float]:
        w = score_tilt_weights(inputs, strength=self.strength)
        parent = float_market_cap_weights(inputs)
        self._last = {
            "n_constituents": len(w),
            "max_weight": max(w.values(), default=0.0),
            "active_exposure": _exposure(w, inputs) - _exposure(parent, inputs),
            "active_share": _active_share(w, parent),
            "tilt_strength": self.strength,
        }
        return w

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._last)


@dataclass
class SelectionWeighter:
    """Take the best fraction by score, then weight within the selection.

    Stronger exposure than a tilt and far easier to explain — "the cheapest 30% of the
    market" is a sentence a trustee understands. It buys that with a cliff edge at the
    selection boundary, which is where the turnover comes from.
    """

    top_fraction: float = 0.30
    weight_by: str = "float_cap"
    name: str = "selection"
    _last: dict[str, Any] = field(default_factory=dict, repr=False)

    def weights(self, inputs: Mapping[str, SecurityInputs],
                context: WeightingContext) -> dict[str, float]:
        w = selection_weights(inputs, top_fraction=self.top_fraction,
                              weight_by=self.weight_by)
        parent = float_market_cap_weights(inputs)
        selected = {k: inputs[k] for k in w}
        self._last = {
            "n_constituents": len(w),
            "n_candidates": len(inputs),
            "max_weight": max(w.values(), default=0.0),
            "active_exposure": _exposure(w, selected) - _exposure(parent, inputs),
            "active_share": _active_share(w, parent),
            "top_fraction": self.top_fraction,
        }
        return w

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._last)


@dataclass
class CapacityConstrainedWeighter:
    """Decorator that trims weights a fund of a given size could not build.

    Applies after the inner weighter. This is what makes a small-cap or deep-value index
    honest about its ceiling: the constraint binds hardest on exactly the names the
    factor most wants to own, which is why capacity has to be a design input rather than
    something discovered after launch.
    """

    inner: Any
    fund_size: float = 5e9
    max_days_to_trade: float = 5.0
    participation: float = 0.20
    name: str = "capacity"
    _last: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.name = f"{getattr(self.inner, 'name', 'inner')}+capacity"

    def weights(self, inputs: Mapping[str, SecurityInputs],
                context: WeightingContext) -> dict[str, float]:
        base = self.inner.weights(inputs, context)
        fund_size = context.fund_size or self.fund_size
        constrained = capacity_constrained_weights(
            inputs, base, fund_size=fund_size,
            max_days_to_trade=self.max_days_to_trade, participation=self.participation,
        )
        trimmed = sum(
            1 for k in base if constrained.get(k, 0.0) < base[k] - 1e-9
        )
        self._last = {
            **self.inner.diagnostics(),
            "fund_size": fund_size,
            "n_trimmed_for_capacity": trimmed,
            "weighted_days_to_trade_before": weighted_average_days_to_trade(
                base, inputs, fund_size, self.participation),
            "weighted_days_to_trade_after": weighted_average_days_to_trade(
                constrained, inputs, fund_size, self.participation),
            "capacity_turnover": sum(
                abs(constrained.get(k, 0.0) - base.get(k, 0.0))
                for k in set(base) | set(constrained)
            ) / 2,
        }
        return constrained

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._last)


@dataclass
class OptimisedWeighter:
    """Maximise factor exposure subject to tracking error, turnover and sector limits.

    The highest factor exposure per unit of active risk, and the hardest to explain —
    "the optimiser did it" is not a client answer, which is why `price_constraints`
    exists to turn the result back into a sentence.

    The risk model is rebuilt at each review from trailing returns ending at the cut-off.
    Ledoit-Wolf rather than sample covariance: at a few hundred candidates from a few
    hundred observations the sample matrix is ill-conditioned, and a minimum-tracking-
    error optimiser hunts precisely the directions whose risk is estimated worst.

    If the problem is infeasible the weighter **falls back to a tilt and says so** in its
    diagnostics. An index that fails to produce weights on a Tuesday is not an index; an
    index that silently changes its weighting scheme without recording it is worse.
    """

    tracking_error_limit: float = 0.03
    max_weight: float = 0.10
    """Must be at least the parent's own concentration limit.

    Set it below the parent's cap and the problem is infeasible on the first review: the
    benchmark itself violates the bound, so pulling a 17%-weight mega-cap down to 5%
    creates a large active position on its own, and no portfolio satisfies both that
    bound and a 3% tracking-error ceiling. That is what happened here, and the optimiser
    fell back to a tilt at every single review while reporting success at the index
    level."""

    max_turnover: float = 0.20
    sector_deviation: float = 0.05
    fallback_tilt_strength: float = 1.0
    min_candidates: int = 30
    capping_config: Any = None
    """Used to build a *capped* benchmark. See `_benchmark`."""

    name: str = "optimised"

    _last: dict[str, Any] = field(default_factory=dict, repr=False)
    n_fallbacks: int = 0
    n_solves: int = 0

    def weights(self, inputs: Mapping[str, SecurityInputs],
                context: WeightingContext) -> dict[str, float]:
        from miniftse.optim.problem import (
            FullInvestment,
            GroupDeviation,
            LongOnly,
            MaximiseExposure,
            ProblemData,
            TrackingError,
            Turnover,
            WeightBounds,
        )
        from miniftse.optim.solve import Optimiser
        from miniftse.risk.covariance import ledoit_wolf

        parent = float_market_cap_weights(inputs)
        ids = list(inputs)

        if len(ids) < self.min_candidates or context.returns is None:
            return self._fallback(inputs, parent, "too few candidates or no returns")

        returns = context.returns.reindex(columns=ids).dropna(axis=1, how="all")
        usable = [c for c in returns.columns if returns[c].notna().sum() >= 60]
        if len(usable) < self.min_candidates:
            return self._fallback(inputs, parent, "insufficient return history")

        try:
            covariance = ledoit_wolf(returns[usable])
        except (ValueError, np.linalg.LinAlgError) as exc:
            return self._fallback(inputs, parent, f"covariance failed: {exc}")

        solve_ids = list(covariance.matrix.index)
        attributes = pd.DataFrame({
            "score": pd.Series({k: inputs[k].score for k in solve_ids}),
            "industry": pd.Series({k: context.industry.get(k, "?") for k in solve_ids}),
        }, index=solve_ids)

        bench = self._benchmark(parent, solve_ids)
        if bench is None:
            return self._fallback(inputs, parent, "benchmark weights sum to zero")

        initial = None
        if context.previous_weights:
            prior = pd.Series({k: context.previous_weights.get(k, 0.0)
                               for k in solve_ids})
            if prior.sum() > 0:
                initial = prior / prior.sum()

        data = ProblemData(
            securities=solve_ids, benchmark=bench, initial_weights=initial,
            covariance=covariance.matrix, attributes=attributes,
            adv=pd.Series({k: inputs[k].adv for k in solve_ids}),
        )
        constraints = [
            FullInvestment(), LongOnly(), WeightBounds(self.max_weight),
            TrackingError(self.tracking_error_limit),
            GroupDeviation("industry", self.sector_deviation),
        ]
        if initial is not None:
            constraints.append(Turnover(self.max_turnover))

        result = Optimiser(MaximiseExposure("score"), constraints).solve(data)
        self.n_solves += 1

        if not result.succeeded:
            return self._fallback(inputs, parent,
                                  f"optimiser status {result.status}")

        weights = {str(k): float(v) for k, v in result.weights.items() if v > 1e-9}
        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}

        self._last = {
            "mode": "optimised",
            "n_constituents": len(weights),
            "max_weight": max(weights.values(), default=0.0),
            "tracking_error": result.tracking_error,
            "turnover": result.turnover,
            "binding_constraints": ", ".join(result.binding_constraints),
            "active_exposure": (
                sum(w * inputs[k].score for k, w in weights.items() if k in inputs)
                - _exposure(parent, inputs)
            ),
            "active_share": _active_share(weights, parent),
            "solver": result.solver,
            "shrinkage": covariance.shrinkage_intensity,
        }
        return weights

    def _benchmark(self, parent: dict[str, float], solve_ids: list[str]
                   ) -> pd.Series | None:
        """The *capped* parent, restricted to the solvable set.

        Capping the benchmark is what guarantees the problem is feasible: the benchmark
        then satisfies the weight bound, sits at zero tracking error and zero sector
        deviation, and is therefore always an admissible point. An optimised index that
        can fail to produce weights on a Tuesday is not an index, and "hold the parent"
        must always be available as the answer of last resort.

        Comparing against the *uncapped* parent instead is the subtle version of the same
        error: the optimiser is then measuring tracking error against a portfolio nobody
        holds.
        """
        from miniftse.weighting.capping import apply_ucits_5_10_40

        raw = {k: parent.get(k, 0.0) for k in solve_ids}
        if sum(raw.values()) <= 0:
            return None
        try:
            capped = apply_ucits_5_10_40(raw, self.capping_config)
            weights = capped.weights
        except Exception:  # noqa: BLE001 - fall back to the uncapped normalisation
            total = sum(raw.values())
            weights = {k: v / total for k, v in raw.items()}
        series = pd.Series(weights).reindex(solve_ids).fillna(0.0)
        total = float(series.sum())
        return series / total if total > 0 else None

    def _fallback(self, inputs: Mapping[str, SecurityInputs],
                  parent: dict[str, float], reason: str) -> dict[str, float]:
        self.n_fallbacks += 1
        w = score_tilt_weights(inputs, strength=self.fallback_tilt_strength)
        self._last = {
            "mode": "tilt_fallback",
            "fallback_reason": reason,
            "n_constituents": len(w),
            "max_weight": max(w.values(), default=0.0),
            "active_exposure": _exposure(w, inputs) - _exposure(parent, inputs),
            "active_share": _active_share(w, parent),
        }
        return w

    def diagnostics(self) -> dict[str, Any]:
        return {**self._last, "n_solves": self.n_solves,
                "n_fallbacks": self.n_fallbacks}


# --------------------------------------------------------------------------------------


def _exposure(weights: dict[str, float], inputs: Mapping[str, SecurityInputs]) -> float:
    return sum(w * inputs[k].score for k, w in weights.items() if k in inputs)


def _active_share(weights: dict[str, float], benchmark: dict[str, float]) -> float:
    """Half the sum of absolute active weights.

    The single most intuitive measure of how different an index is from its parent, and
    the one clients ask for by name. Unlike tracking error it needs no risk model, so it
    cannot be wrong for reasons the client cannot inspect.
    """
    keys = set(weights) | set(benchmark)
    return sum(abs(weights.get(k, 0.0) - benchmark.get(k, 0.0)) for k in keys) / 2.0


WEIGHTER_REGISTRY: dict[str, Any] = {
    "float_cap": FloatCapWeighter,
    "tilt": TiltWeighter,
    "selection": SelectionWeighter,
    "optimised": OptimisedWeighter,
}
