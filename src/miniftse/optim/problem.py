"""Declarative constrained portfolio optimisation on cvxpy.

Two things make an optimiser useful in production, and neither is the maths:

1. **Constraints are objects, not code.** A constraint you can name, serialise, relax
   and price is a constraint you can explain in a methodology document and audit later.
   Constraints written inline as cvxpy expressions are none of those things.
2. **Infeasibility is diagnosable.** "Solver status: infeasible" is not an answer.
   The practical skill is saying *which pair of constraints conflicts* and what it would
   cost to relax each - and that is what `diagnose` does.

The objective is convex throughout. The square-root market-impact term is concave in
traded quantity and therefore convex as a cost, which is the technical reason the
square-root law is convenient as well as empirically right. Count constraints ("hold
exactly 100 names") are genuinely non-convex and are handled by a documented heuristic
rather than pretended into the convex problem.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import cvxpy as cp
import numpy as np
import pandas as pd


class OptimisationError(RuntimeError):
    pass


@dataclass
class ProblemData:
    """Everything an optimisation needs, aligned on a common security index."""

    securities: list[str]
    benchmark: pd.Series
    initial_weights: pd.Series | None = None
    expected_returns: pd.Series | None = None
    covariance: pd.DataFrame | None = None
    factor_exposures: pd.DataFrame | None = None
    factor_covariance: pd.DataFrame | None = None
    specific_variance: pd.Series | None = None
    adv: pd.Series | None = None
    attributes: pd.DataFrame = field(default_factory=pd.DataFrame)
    """Arbitrary per-security attributes constraints can reference - carbon intensity,
    industry, country, factor scores."""

    def align(self, series: pd.Series | None, default: float = 0.0) -> np.ndarray:
        if series is None:
            return np.full(len(self.securities), default)
        return series.reindex(self.securities).fillna(default).to_numpy(dtype=float)

    def risk_quadratic_form(self, w: cp.Variable) -> cp.Expression:
        """Portfolio variance, using the factor model when available.

        The factor form is not merely faster. A full N x N covariance from a factor
        model is dense and its Cholesky is O(N^3); expressing risk as
        ``(B'w)' F (B'w) + w'Dw`` keeps the problem at K factors, so an optimisation
        over thousands of names stays tractable.
        """
        if self.factor_exposures is not None and self.factor_covariance is not None:
            b = self.factor_exposures.reindex(self.securities).fillna(0.0).to_numpy()
            f = self.factor_covariance.to_numpy()
            d = self.align(self.specific_variance, 1e-6)
            factor_risk = cp.quad_form(b.T @ w, cp.psd_wrap(f))
            return factor_risk + cp.sum(cp.multiply(d, cp.square(w)))
        if self.covariance is not None:
            sigma = (
                self.covariance.reindex(index=self.securities, columns=self.securities)
                .fillna(0.0)
                .to_numpy()
            )
            return cp.quad_form(w, cp.psd_wrap(sigma))
        raise OptimisationError("no risk model supplied")


# --------------------------------------------------------------------------------------
# Constraints
# --------------------------------------------------------------------------------------


class Constraint(ABC):
    """A named, relaxable constraint."""

    name: str
    is_hard: bool = True
    """Hard constraints are never relaxed by the diagnostics. Full investment and
    long-only usually are; a turnover budget usually is not."""

    @abstractmethod
    def build(self, w: cp.Variable, data: ProblemData) -> list[cp.Constraint]: ...

    @abstractmethod
    def relaxed(self, factor: float) -> Constraint:
        """A version loosened by `factor`, for infeasibility diagnosis and for pricing
        the constraint in tracking-error terms."""

    def describe(self) -> str:
        return self.name


@dataclass
class FullInvestment(Constraint):
    name: str = "full_investment"
    is_hard: bool = True
    target: float = 1.0

    def build(self, w: cp.Variable, data: ProblemData) -> list[cp.Constraint]:
        del data
        return [cp.sum(w) == self.target]

    def relaxed(self, factor: float) -> FullInvestment:
        del factor
        return self

    def describe(self) -> str:
        return f"Weights sum to {self.target:.0%}."


@dataclass
class LongOnly(Constraint):
    name: str = "long_only"
    is_hard: bool = True

    def build(self, w: cp.Variable, data: ProblemData) -> list[cp.Constraint]:
        del data
        return [w >= 0]

    def relaxed(self, factor: float) -> LongOnly:
        del factor
        return self

    def describe(self) -> str:
        return "No short positions."


@dataclass
class WeightBounds(Constraint):
    max_weight: float = 0.05
    min_weight: float = 0.0
    name: str = "weight_bounds"
    is_hard: bool = False

    def build(self, w: cp.Variable, data: ProblemData) -> list[cp.Constraint]:
        del data
        return [w <= self.max_weight, w >= self.min_weight]

    def relaxed(self, factor: float) -> WeightBounds:
        return WeightBounds(self.max_weight * factor, self.min_weight, self.name)

    def describe(self) -> str:
        return f"No single holding above {self.max_weight:.1%}."


@dataclass
class TrackingError(Constraint):
    """Ex-ante active risk ceiling.

    Written as a variance constraint rather than a standard-deviation one because
    variance is a quadratic form and therefore convex; the square root of a quadratic
    form is convex too, but SOCP formulations solve less reliably here.
    """

    max_te: float = 0.02
    name: str = "tracking_error"
    is_hard: bool = False
    annualise: bool = True

    def build(self, w: cp.Variable, data: ProblemData) -> list[cp.Constraint]:
        active = w - data.align(data.benchmark)
        daily_var = (self.max_te**2 / 252) if self.annualise else self.max_te**2
        return [data.risk_quadratic_form(active) <= daily_var]

    def relaxed(self, factor: float) -> TrackingError:
        return TrackingError(self.max_te * factor, self.name, self.is_hard, self.annualise)

    def describe(self) -> str:
        return f"Ex-ante tracking error at most {self.max_te:.2%} a year."


@dataclass
class Turnover(Constraint):
    """One-way turnover budget: ``0.5 * ||w - w0||_1 <= tau``.

    The single most effective constraint for making an optimised index investable. An
    unconstrained optimiser rebuilds the portfolio every rebalance because tiny changes
    in estimated inputs move the solution, and the tracking funds pay for all of it.
    """

    max_turnover: float = 0.15
    name: str = "turnover"
    is_hard: bool = False

    def build(self, w: cp.Variable, data: ProblemData) -> list[cp.Constraint]:
        if data.initial_weights is None:
            return []
        w0 = data.align(data.initial_weights)
        return [0.5 * cp.norm1(w - w0) <= self.max_turnover]

    def relaxed(self, factor: float) -> Turnover:
        return Turnover(self.max_turnover * factor, self.name, self.is_hard)

    def describe(self) -> str:
        return f"One-way turnover at most {self.max_turnover:.1%} per rebalance."


@dataclass
class GroupDeviation(Constraint):
    """Active weight per group (industry, country) bounded on both sides."""

    group_column: str
    max_deviation: float = 0.02
    name: str = "group_deviation"
    is_hard: bool = False

    def build(self, w: cp.Variable, data: ProblemData) -> list[cp.Constraint]:
        if self.group_column not in data.attributes.columns:
            return []
        groups = data.attributes[self.group_column].reindex(data.securities)
        bench = data.align(data.benchmark)
        out: list[cp.Constraint] = []
        for value in groups.dropna().unique():
            mask = (groups == value).to_numpy(dtype=float)
            active = mask @ w - float(mask @ bench)
            out += [active <= self.max_deviation, active >= -self.max_deviation]
        return out

    def relaxed(self, factor: float) -> GroupDeviation:
        return GroupDeviation(
            self.group_column, self.max_deviation * factor, self.name, self.is_hard
        )

    def describe(self) -> str:
        return (
            f"Active weight in any {self.group_column} within "
            f"+/-{self.max_deviation:.1%} of the benchmark."
        )


@dataclass
class FactorExposure(Constraint):
    """Bound the portfolio's exposure to a named attribute."""

    attribute: str
    minimum: float | None = None
    maximum: float | None = None
    relative_to_benchmark: bool = True
    name: str = "factor_exposure"
    is_hard: bool = False

    def build(self, w: cp.Variable, data: ProblemData) -> list[cp.Constraint]:
        if self.attribute not in data.attributes.columns:
            return []
        x = data.attributes[self.attribute].reindex(data.securities).fillna(0.0)
        xv = x.to_numpy(dtype=float)
        expr = xv @ w
        if self.relative_to_benchmark:
            expr = expr - float(xv @ data.align(data.benchmark))
        out: list[cp.Constraint] = []
        if self.minimum is not None:
            out.append(expr >= self.minimum)
        if self.maximum is not None:
            out.append(expr <= self.maximum)
        return out

    def relaxed(self, factor: float) -> FactorExposure:
        return FactorExposure(
            self.attribute,
            self.minimum / factor if self.minimum is not None else None,
            self.maximum * factor if self.maximum is not None else None,
            self.relative_to_benchmark,
            self.name,
            self.is_hard,
        )

    def describe(self) -> str:
        bits = []
        if self.minimum is not None:
            bits.append(f"at least {self.minimum:+.3f}")
        if self.maximum is not None:
            bits.append(f"at most {self.maximum:+.3f}")
        rel = " relative to the benchmark" if self.relative_to_benchmark else ""
        return f"Exposure to {self.attribute} {' and '.join(bits)}{rel}."


@dataclass
class IntensityReduction(Constraint):
    """Weighted-average intensity below a fraction of the benchmark's.

    The Climate Transition and Paris-Aligned benchmark constraint. Written as a linear
    inequality on weights, which it is once the benchmark intensity is a known constant:
    ``sum(w_i * c_i) <= (1 - reduction) * sum(b_i * c_i)``.
    """

    attribute: str = "carbon_intensity"
    reduction: float = 0.50
    name: str = "intensity_reduction"
    is_hard: bool = False

    def build(self, w: cp.Variable, data: ProblemData) -> list[cp.Constraint]:
        if self.attribute not in data.attributes.columns:
            return []
        c = data.attributes[self.attribute].reindex(data.securities).fillna(0.0)
        cv = c.to_numpy(dtype=float)
        benchmark_intensity = float(cv @ data.align(data.benchmark))
        return [cv @ w <= (1.0 - self.reduction) * benchmark_intensity]

    def relaxed(self, factor: float) -> IntensityReduction:
        return IntensityReduction(self.attribute, self.reduction / factor, self.name, self.is_hard)

    def describe(self) -> str:
        return (
            f"Weighted-average {self.attribute} at least {self.reduction:.0%} below the benchmark."
        )


@dataclass
class LiquidityCap(Constraint):
    """Cap each weight at a multiple of the name's share of universe liquidity.

    Prevents the optimiser doing the thing it always wants to do: put a large weight in
    a small illiquid name because the risk model, estimated on stale prices, thinks it
    is uncorrelated with everything.
    """

    multiple: float = 5.0
    name: str = "liquidity_cap"
    is_hard: bool = False

    def build(self, w: cp.Variable, data: ProblemData) -> list[cp.Constraint]:
        if data.adv is None:
            return []
        adv = data.align(data.adv)
        total = adv.sum()
        if total <= 0:
            return []
        return [w <= self.multiple * adv / total]

    def relaxed(self, factor: float) -> LiquidityCap:
        return LiquidityCap(self.multiple * factor, self.name, self.is_hard)

    def describe(self) -> str:
        return (
            f"No holding above {self.multiple:.0f}x the security's share of universe traded value."
        )


# --------------------------------------------------------------------------------------
# Objectives
# --------------------------------------------------------------------------------------


class Objective(ABC):
    name: str

    @abstractmethod
    def build(self, w: cp.Variable, data: ProblemData) -> cp.Expression: ...


@dataclass
class MinimiseVariance(Objective):
    name: str = "min_variance"

    def build(self, w: cp.Variable, data: ProblemData) -> cp.Expression:
        return data.risk_quadratic_form(w)


@dataclass
class MinimiseTrackingError(Objective):
    name: str = "min_tracking_error"

    def build(self, w: cp.Variable, data: ProblemData) -> cp.Expression:
        return data.risk_quadratic_form(w - data.align(data.benchmark))


@dataclass
class MaximiseExposure(Objective):
    """Maximise exposure to an attribute, usually subject to a tracking-error cap.

    The standard formulation for a factor index built by optimisation: get as much
    factor as possible for a fixed active-risk budget.
    """

    attribute: str
    name: str = "max_exposure"

    def build(self, w: cp.Variable, data: ProblemData) -> cp.Expression:
        x = data.attributes[self.attribute].reindex(data.securities).fillna(0.0)
        return -(x.to_numpy(dtype=float) @ w)


@dataclass
class MeanVarianceUtility(Objective):
    """`-mu'w + (lambda/2) w'Sigma w`, the classic and the fragile one.

    Michaud's "error maximiser": expected returns are estimated with enormous error, and
    the optimiser systematically overweights whatever was most overestimated. Provided
    for completeness; the exposure-maximising formulation above is what an index
    actually uses, because it needs a *ranking*, not a return forecast.
    """

    risk_aversion: float = 5.0
    name: str = "mean_variance"

    def build(self, w: cp.Variable, data: ProblemData) -> cp.Expression:
        mu = data.align(data.expected_returns)
        return -(mu @ w) + (self.risk_aversion / 2) * data.risk_quadratic_form(w)


@dataclass
class TransactionCostPenalty:
    """Linear plus square-root market impact, added to any objective.

    ``cost = linear * |dw| + impact * |dw|^1.5 / sqrt(participation)``

    The 3/2 power is the integral of the square-root impact law over the trade, and
    `cp.power(x, 1.5)` is convex - so cost-aware optimisation stays a convex problem.
    A linear-only cost model systematically underestimates the cost of large trades and
    lets the optimiser propose rebalances no one can execute.
    """

    linear_bps: float = 5.0
    impact_coefficient: float = 0.10
    weight: float = 1.0

    def build(self, w: cp.Variable, data: ProblemData) -> cp.Expression:
        if data.initial_weights is None:
            return cp.Constant(0.0)
        w0 = data.align(data.initial_weights)
        trade = cp.abs(w - w0)
        linear = (self.linear_bps / 10_000) * cp.sum(trade)
        if data.adv is None or self.impact_coefficient <= 0:
            return self.weight * linear
        adv = data.align(data.adv, 1.0)
        scale = np.clip(adv / max(adv.sum(), 1e-12), 1e-8, None)
        impact = self.impact_coefficient * cp.sum(cp.power(trade, 1.5) / np.sqrt(scale))
        return self.weight * (linear + impact)
