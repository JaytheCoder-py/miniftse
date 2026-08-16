"""Concentration capping.

The naive algorithm - "cap the biggest name, renormalise the rest" - is wrong, and
wrong in a way that looks right on ordinary data. Renormalising pushes weight onto the
remaining names, which can push the *second* name above the cap; capping that one pushes
weight again; and with enough concentration the loop oscillates instead of converging.

The correct algorithm holds capped names fixed and redistributes only among the
uncapped, iterating to a fixed point. That is `cap_weights` below.

The UCITS 5/10/40 rule is harder still, because the second limb is a constraint on a
*set*: the names individually above 5% must together be at most 40%. Which names are in
that set depends on the weights, which depend on the capping. `apply_ucits_5_10_40`
handles the interaction explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from miniftse.config import CappingConfig


class CappingError(RuntimeError):
    """Capping could not converge, or the constraints are infeasible."""


@dataclass(frozen=True, slots=True)
class CappingResult:
    weights: dict[str, float]
    factors: dict[str, float]
    """Multiplicative factor applied to each raw weight. This is what the index stores
    as `C_i`, because the divisor formula needs the factor, not the resulting weight."""

    iterations: int
    capped_names: tuple[str, ...]
    converged: bool
    max_weight: float
    aggregate_above_threshold: float
    notes: tuple[str, ...] = ()

    def turnover_vs(self, other: dict[str, float]) -> float:
        keys = set(self.weights) | set(other)
        return sum(abs(self.weights.get(k, 0.0) - other.get(k, 0.0)) for k in keys) / 2.0


def cap_weights(
    weights: dict[str, float],
    cap: float,
    *,
    max_iterations: int = 100,
    tolerance: float = 1e-10,
) -> CappingResult:
    """Iteratively cap to a single maximum weight.

    Fixed-point iteration: names at the cap are frozen, the remainder are rescaled to
    fill the residual, and any that breach as a result are frozen too. Terminates
    because the frozen set grows monotonically and is bounded by the number of names.

    Infeasible when ``cap * n < 1``: you cannot fit unit weight into n names each
    limited to `cap`. Raised rather than silently returning weights that sum to less
    than one.
    """
    names = list(weights)
    n = len(names)
    if n == 0:
        return CappingResult({}, {}, 0, (), True, 0.0, 0.0, ("empty universe",))
    if cap * n < 1.0 - tolerance:
        raise CappingError(
            f"infeasible: {n} names capped at {cap:.4f} can hold at most {cap * n:.4f} of the index"
        )

    raw = np.array([weights[k] for k in names], dtype=float)
    if raw.sum() <= 0:
        raise CappingError("weights must sum to a positive number")
    w = raw / raw.sum()

    capped = np.zeros(n, dtype=bool)
    iterations = 0

    for iterations in range(1, max_iterations + 1):  # noqa: B007 - reported after the loop
        breaching = (w > cap + tolerance) & ~capped
        if not breaching.any():
            break

        capped |= breaching
        w[capped] = cap

        residual = 1.0 - cap * capped.sum()
        free_mass = w[~capped].sum()

        if free_mass <= tolerance:
            n_free = int((~capped).sum())
            if n_free == 0:
                if abs(residual) > tolerance:
                    raise CappingError("every name is at the cap but the weights do not sum to one")
                break
            # Free names exist but carry essentially no weight, so proportional
            # rescaling is undefined (0/0). Distribute the residual equally instead.
            #
            # Found by a property test with one name at 0.9999999999 and ten at 1e-11.
            # Contrived, but the real version is not: a security suspended at a nominal
            # price alongside a mega-cap produces exactly this shape, and the previous
            # code raised on it rather than capping.
            w[~capped] = residual / n_free
            continue

        # Rescale only the uncapped names. Rescaling everything is the classic bug:
        # it lifts the capped names back above the cap.
        w[~capped] *= residual / free_mass
    else:
        raise CappingError(
            f"capping did not converge in {max_iterations} iterations "
            f"(max weight {w.max():.6f} vs cap {cap:.6f})"
        )

    factors = {
        name: (w[i] / weights[name] * raw.sum() if weights[name] > 0 else 1.0)
        for i, name in enumerate(names)
    }
    return CappingResult(
        weights={name: float(w[i]) for i, name in enumerate(names)},
        factors=factors,
        iterations=iterations,
        capped_names=tuple(names[i] for i in np.flatnonzero(capped)),
        converged=True,
        max_weight=float(w.max()),
        aggregate_above_threshold=0.0,
    )


def apply_ucits_5_10_40(
    weights: dict[str, float],
    config: CappingConfig | None = None,
) -> CappingResult:
    """The UCITS diversification rule, as index providers implement it.

    Two limbs:

    1. No single holding above 10%.
    2. Holdings that individually exceed 5% must together be at most 40%.

    Limb 2 is the awkward one. It is a constraint on a set whose membership depends on
    the answer, so it cannot be solved in one pass. The approach here: apply limb 1,
    then while limb 2 is breached, lower the *effective* cap on the names above 5% and
    re-run limb 1. Binary search on the effective cap converges quickly and, unlike an
    ad-hoc scaling, never leaves limb 1 breached.

    A boundary case worth knowing: if enough names sit above 5% that the 40% bucket
    binds hard, the effective cap can fall below 5%, at which point no name is in the
    bucket any more and the constraint is trivially satisfied. The loop handles this
    by stopping once the effective cap reaches the 5% threshold.
    """
    cfg = config or CappingConfig()
    if not cfg.enabled:
        total = sum(weights.values())
        norm = {k: v / total for k, v in weights.items()} if total else {}
        return CappingResult(
            norm,
            dict.fromkeys(weights, 1.0),
            0,
            (),
            True,
            max(norm.values(), default=0.0),
            0.0,
            ("capping disabled",),
        )

    notes: list[str] = []
    result = cap_weights(
        weights, cfg.max_single_weight, max_iterations=cfg.max_iterations, tolerance=cfg.tolerance
    )

    def aggregate_above(w: dict[str, float]) -> float:
        return sum(v for v in w.values() if v > cfg.aggregate_threshold + cfg.tolerance)

    agg = aggregate_above(result.weights)
    if agg <= cfg.aggregate_limit + cfg.tolerance:
        return CappingResult(
            result.weights,
            result.factors,
            result.iterations,
            result.capped_names,
            True,
            result.max_weight,
            agg,
            (
                f"limb 1 satisfied at {cfg.max_single_weight:.0%}; limb 2 not binding "
                f"({agg:.2%} <= {cfg.aggregate_limit:.0%})",
            ),
        )

    notes.append(
        f"limb 2 breached: names above {cfg.aggregate_threshold:.0%} sum to {agg:.2%}, "
        f"limit {cfg.aggregate_limit:.0%}"
    )

    # Binary search for the LARGEST effective cap that satisfies limb 2.
    #
    # Aggregate weight above the 5% threshold is non-decreasing in the cap, so the
    # feasible set is an interval [floor, c*] and the search is for its upper end. The
    # direction matters and is easy to get backwards: an earlier version raised `lo`
    # when the constraint was breached, so a breach made it try an even *higher* cap
    # and the search walked away from the answer. It returned the uncapped weights and
    # reported success.
    #
    # Least restrictive is the right objective: any cap below c* also satisfies the
    # rule, but each one distorts the index further from its stated weighting scheme
    # than necessary.
    lo, hi = cfg.aggregate_threshold, cfg.max_single_weight
    try:
        best = cap_weights(weights, lo, max_iterations=cfg.max_iterations, tolerance=cfg.tolerance)
    except CappingError:
        # Even the floor is infeasible - too few constituents to spread unit weight.
        best = result
        notes.append(
            f"the {cfg.aggregate_threshold:.0%} floor is itself infeasible for "
            f"{len(weights)} constituents; 5/10/40 cannot be satisfied by this universe"
        )
        return CappingResult(
            best.weights,
            best.factors,
            best.iterations,
            best.capped_names,
            False,
            best.max_weight,
            agg,
            tuple(notes),
        )

    for _ in range(60):
        mid = (lo + hi) / 2.0
        try:
            trial = cap_weights(
                weights, mid, max_iterations=cfg.max_iterations, tolerance=cfg.tolerance
            )
        except CappingError:
            hi = mid
            continue
        if aggregate_above(trial.weights) <= cfg.aggregate_limit + cfg.tolerance:
            best, lo = trial, mid  # feasible: try a less restrictive cap
        else:
            hi = mid  # breached: the cap must come down
        if hi - lo < 1e-9:
            break

    final_agg = aggregate_above(best.weights)
    satisfied = final_agg <= cfg.aggregate_limit + 1e-6
    notes.append(
        f"effective cap lowered to {lo:.4%}; names above "
        f"{cfg.aggregate_threshold:.0%} now sum to {final_agg:.2%}"
    )
    if not satisfied:
        notes.append(
            "limb 2 remains breached even at the 5% floor - the universe is too "
            "concentrated for 5/10/40 and needs either more constituents or a "
            "different diversification standard"
        )

    return CappingResult(
        best.weights,
        best.factors,
        best.iterations,
        best.capped_names,
        satisfied,
        best.max_weight,
        final_agg,
        tuple(notes),
    )


def cap_by_group(
    weights: dict[str, float],
    groups: dict[str, str],
    group_cap: float,
    *,
    max_iterations: int = 100,
    tolerance: float = 1e-10,
) -> CappingResult:
    """Cap aggregate weight per group - country, industry, or issuer.

    Issuer capping is the one that matters for dual-class names: a 10% issuer cap on
    Alphabet must see GOOGL and GOOG as one object, and applying a security-level cap
    to each gives the issuer 20%.
    """
    total = sum(weights.values())
    if total <= 0:
        raise CappingError("weights must sum to a positive number")
    w = {k: v / total for k, v in weights.items()}

    group_members: dict[str, list[str]] = {}
    for name, g in groups.items():
        if name in w:
            group_members.setdefault(g, []).append(name)

    if group_cap * len(group_members) < 1.0 - tolerance:
        raise CappingError(f"infeasible: {len(group_members)} groups capped at {group_cap:.4f}")

    frozen: set[str] = set()
    iterations = 0
    for iterations in range(1, max_iterations + 1):  # noqa: B007 - reported after the loop
        totals = {g: sum(w[n] for n in members) for g, members in group_members.items()}
        breaching = [
            g
            for g, t in totals.items()
            if t > group_cap + tolerance and not set(group_members[g]) <= frozen
        ]
        if not breaching:
            break
        for g in breaching:
            members = group_members[g]
            scale = group_cap / totals[g]
            for n in members:
                w[n] *= scale
            frozen.update(members)

        free = [n for n in w if n not in frozen]
        free_mass = sum(w[n] for n in free)
        residual = 1.0 - sum(w[n] for n in frozen)
        if free_mass <= tolerance:
            break
        for n in free:
            w[n] *= residual / free_mass
    else:
        raise CappingError(f"group capping did not converge in {max_iterations} iterations")

    factors = {k: (w[k] * total / weights[k] if weights[k] > 0 else 1.0) for k in weights}
    group_totals = {g: sum(w[n] for n in m) for g, m in group_members.items()}
    return CappingResult(
        weights=w,
        factors=factors,
        iterations=iterations,
        capped_names=tuple(sorted(frozen)),
        converged=True,
        max_weight=max(w.values(), default=0.0),
        aggregate_above_threshold=max(group_totals.values(), default=0.0),
        notes=(f"{len(frozen)} names in {len(set(groups[n] for n in frozen))} capped groups",),
    )


def verify_capping(weights: dict[str, float], cap: float, tolerance: float = 1e-9) -> list[str]:
    """Post-conditions a capped weight vector must satisfy. Used by the property tests
    and by the pre-publication validation gate."""
    problems: list[str] = []
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-8:
        problems.append(f"weights sum to {total:.12f}, not 1.0")
    for name, w in weights.items():
        if w > cap + tolerance:
            problems.append(f"{name} at {w:.6%} exceeds the {cap:.2%} cap")
        if w < -tolerance:
            problems.append(f"{name} has negative weight {w:.6%}")
    return problems
