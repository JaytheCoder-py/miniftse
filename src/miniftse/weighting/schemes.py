"""Weighting schemes.

Each scheme is a different answer to "what should this index be *for*", and each buys
its factor exposure with turnover, capacity, or explainability. The comparison table in
`SCHEME_PROPERTIES` is the thing to have in your head in an interview: the maths is
trivial, the trade-offs are the job.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import numpy as np


class WeightingScheme(Protocol):
    """Turn a set of candidate securities into weights summing to one."""

    name: str

    def compute(self, inputs: Mapping[str, SecurityInputs]) -> dict[str, float]: ...


@dataclass(frozen=True, slots=True)
class SecurityInputs:
    """Everything any scheme in this module might need."""

    security_id: str
    price: float
    shares: float
    free_float_factor: float = 1.0
    fx_rate: float = 1.0
    adv: float = 0.0
    """Average daily traded value in base currency. Drives capacity constraints."""

    score: float = 0.0
    """Factor score, for tilt and selection schemes."""

    volatility: float = 0.0
    fundamental_size: float = 0.0
    """A non-price measure of company size - sales, book, dividends - for
    fundamental weighting."""

    @property
    def full_market_cap(self) -> float:
        return self.price * self.shares * self.fx_rate

    @property
    def float_market_cap(self) -> float:
        return self.full_market_cap * self.free_float_factor


def _normalise(raw: dict[str, float]) -> dict[str, float]:
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("cannot normalise a non-positive weight vector")
    return {k: v / total for k, v in raw.items()}


# --------------------------------------------------------------------------------------


def full_market_cap_weights(inputs: Mapping[str, SecurityInputs]) -> dict[str, float]:
    """Weight by total market cap, ignoring free float.

    Almost never right for an investable index: it over-weights companies whose shares
    are mostly locked up in government or founder hands, which a tracking fund cannot
    buy. Included because it is the correct denominator for measuring how much
    float-adjustment actually changed.
    """
    return _normalise({k: v.full_market_cap for k, v in inputs.items()})


def float_market_cap_weights(inputs: Mapping[str, SecurityInputs]) -> dict[str, float]:
    """The default for a broad benchmark.

    Weighting by investable market value means the index is, in aggregate, exactly what
    all investors hold - so it is replicable at scale with near-zero turnover from price
    drift. That self-rebalancing property is why cap weighting dominates despite having
    no theoretical claim to optimality.
    """
    return _normalise({k: v.float_market_cap for k, v in inputs.items()})


def equal_weights(inputs: Mapping[str, SecurityInputs]) -> dict[str, float]:
    """Equal weight.

    Implicit small-cap and value tilt, and a hard capacity ceiling: the smallest
    constituent gets the same weight as the largest, so the fund's size is bounded by
    what it can buy of the smallest name. Turnover is structural - prices drift apart
    between rebalances and must be traded back - which is the opposite of cap weighting.
    """
    n = len(inputs)
    if n == 0:
        raise ValueError("empty universe")
    return dict.fromkeys(inputs, 1.0 / n)


def fundamental_weights(inputs: Mapping[str, SecurityInputs]) -> dict[str, float]:
    """Weight by an accounting measure of size rather than by price.

    The point is that the weight does not move when the price does, which breaks the
    link between weight and valuation and produces a value tilt as a by-product. It also
    means the index must trade at every rebalance, because prices have moved and the
    weights have not.
    """
    return _normalise({k: max(v.fundamental_size, 0.0) for k, v in inputs.items()})


def inverse_volatility_weights(inputs: Mapping[str, SecurityInputs]) -> dict[str, float]:
    """Risk-weighted, ignoring correlations.

    A crude risk parity: cheap, transparent, and wrong whenever correlations matter -
    which is whenever there is a sector concentration. `optim` has the version that
    uses the full covariance matrix.
    """
    raw = {k: (1.0 / v.volatility if v.volatility > 0 else 0.0) for k, v in inputs.items()}
    if sum(raw.values()) <= 0:
        raise ValueError("no positive volatilities supplied")
    return _normalise(raw)


def score_tilt_weights(
    inputs: Mapping[str, SecurityInputs],
    *,
    strength: float = 1.0,
    floor: float = 0.0,
) -> dict[str, float]:
    """Cap-weight multiplied by a function of the factor score:

        w_i ∝ w_i^cap × f(score_i),    f(s) = exp(strength × s)

    The FTSE Russell "tilt" family works this way, and it is the most capacity-friendly
    route from a signal to an index. Every eligible name keeps a position, so turnover
    comes only from score changes rather than from names entering and leaving; the price
    is weaker factor exposure than a selection or optimised approach would give.

    An exponential rather than a linear tilt because it cannot produce a negative
    weight, needs no clipping, and gives a constant proportional response to a one-unit
    change in score anywhere on the distribution.
    """
    base = float_market_cap_weights(inputs)
    tilted = {
        k: base[k] * float(np.exp(strength * inputs[k].score)) + floor * base[k] for k in base
    }
    return _normalise(tilted)


def selection_weights(
    inputs: Mapping[str, SecurityInputs],
    *,
    top_fraction: float = 0.30,
    weight_by: str = "float_cap",
) -> dict[str, float]:
    """Select the top fraction by score, then weight within the selection.

    Higher factor exposure than a tilt and far easier to explain to a client ("the
    cheapest 30% of the market"). It buys that with a cliff edge: a name one rank
    outside the cut gets zero, so small score changes cause large turnover. Buffers
    exist to soften exactly this.
    """
    if not 0 < top_fraction <= 1:
        raise ValueError("top_fraction must be in (0, 1]")
    ordered = sorted(inputs.values(), key=lambda x: x.score, reverse=True)
    n_select = max(1, int(round(len(ordered) * top_fraction)))
    selected = {x.security_id: x for x in ordered[:n_select]}

    match weight_by:
        case "float_cap":
            return float_market_cap_weights(selected)
        case "equal":
            return equal_weights(selected)
        case "score":
            shifted = {
                k: max(v.score - min(s.score for s in selected.values()) + 1e-6, 0.0)
                for k, v in selected.items()
            }
            return _normalise(shifted)
        case _:
            raise ValueError(f"unknown weight_by {weight_by!r}")


def capacity_constrained_weights(
    inputs: Mapping[str, SecurityInputs],
    base: dict[str, float],
    *,
    fund_size: float,
    max_days_to_trade: float = 5.0,
    participation: float = 0.20,
) -> dict[str, float]:
    """Trim weights that a fund of a given size could not build in reasonable time.

    Days-to-trade for a name is ``fund_size * w / (ADV * participation)``. Capping that
    at `max_days_to_trade` gives a hard ceiling on the weight, and the residual is
    redistributed to names with headroom.

    This is what makes a small-cap value index capacity-limited: the constraint binds on
    exactly the names the factor most wants to own.
    """
    if fund_size <= 0:
        return dict(base)

    ceilings = {
        k: (
            inputs[k].adv * participation * max_days_to_trade / fund_size
            if inputs[k].adv > 0
            else 0.0
        )
        for k in base
    }
    w = dict(base)
    frozen: set[str] = set()
    for _ in range(100):
        breaching = [k for k in w if k not in frozen and w[k] > ceilings[k]]
        if not breaching:
            break
        for k in breaching:
            w[k] = ceilings[k]
            frozen.add(k)
        residual = 1.0 - sum(w[k] for k in frozen)
        free = [k for k in w if k not in frozen]
        free_mass = sum(w[k] for k in free)
        if free_mass <= 0 or residual <= 0:
            break
        for k in free:
            w[k] *= residual / free_mass
    return _normalise(w)


def days_to_trade(
    weights: dict[str, float],
    inputs: Mapping[str, SecurityInputs],
    fund_size: float,
    participation: float = 0.20,
) -> dict[str, float]:
    """Per-name days to build the position at a given participation rate."""
    return {
        k: (fund_size * w / (inputs[k].adv * participation) if inputs[k].adv > 0 else float("inf"))
        for k, w in weights.items()
    }


def weighted_average_days_to_trade(
    weights: dict[str, float],
    inputs: Mapping[str, SecurityInputs],
    fund_size: float,
    participation: float = 0.20,
) -> float:
    """The single number to quote for index capacity."""
    dtt = days_to_trade(weights, inputs, fund_size, participation)
    finite = {k: v for k, v in dtt.items() if np.isfinite(v)}
    if not finite:
        return float("inf")
    return sum(weights[k] * finite[k] for k in finite) / sum(weights[k] for k in finite)


# --------------------------------------------------------------------------------------

SCHEME_PROPERTIES: dict[str, dict[str, str]] = {
    "float_market_cap": {
        "turnover": "lowest - self-rebalancing as prices move",
        "capacity": "highest - weights track what the market actually holds",
        "factor_exposure": "none by construction",
        "explainability": "highest",
        "use_when": "a broad benchmark that must be replicable at any scale",
    },
    "equal": {
        "turnover": "high - prices drift apart and must be traded back",
        "capacity": "low - bounded by the smallest constituent",
        "factor_exposure": "incidental small and value tilt",
        "explainability": "high",
        "use_when": "concentration in the parent is the problem being solved",
    },
    "fundamental": {
        "turnover": "moderate - rebalances against price moves",
        "capacity": "high",
        "factor_exposure": "value tilt as a by-product, not by design",
        "explainability": "moderate - needs the 'weight does not follow price' argument",
        "use_when": "the client objects to weighting by valuation",
    },
    "score_tilt": {
        "turnover": "low to moderate - every name keeps a position",
        "capacity": "high - stays close to cap weights",
        "factor_exposure": "modest, tunable with the tilt strength",
        "explainability": "high",
        "use_when": "cheap factor exposure with a strict turnover budget",
    },
    "selection": {
        "turnover": "high - cliff edge at the selection boundary",
        "capacity": "moderate - concentrates into a subset",
        "factor_exposure": "strong",
        "explainability": "highest of the factor approaches",
        "use_when": "the client wants a clean story and can absorb the turnover",
    },
    "optimised": {
        "turnover": "controllable - it is an explicit constraint",
        "capacity": "controllable - likewise",
        "factor_exposure": "highest per unit of tracking error",
        "explainability": "lowest - 'the optimiser did it' is not a client answer",
        "use_when": "several constraints must hold simultaneously, e.g. a climate index",
    },
}
"""The trade-off triangle, made concrete: factor exposure, turnover/capacity, and
explainability. No scheme wins on all three, and which two you pick is a product
decision rather than a quantitative one."""
