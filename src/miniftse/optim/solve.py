"""Solving, and diagnosing what happened when it fails.

The diagnostics are the point. A solver that returns "infeasible" has told you nothing
actionable; a research platform has to answer *which constraints conflict* and *what
each one costs*. Two tools here do that:

* ``diagnose_infeasible`` - relax constraints one at a time and in pairs until the
  problem solves, and report the minimal set responsible.
* ``price_constraints`` - solve repeatedly with each constraint relaxed and report the
  tracking-error improvement. This is the table that goes to the client: "the 2% sector
  deviation limit costs you 18bp of tracking error, the carbon reduction costs 34bp."
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import cvxpy as cp
import numpy as np
import pandas as pd

from miniftse.optim.problem import (
    Constraint,
    Objective,
    OptimisationError,
    ProblemData,
    TransactionCostPenalty,
)

SOLVERS = ("CLARABEL", "ECOS", "SCS", "OSQP")
"""Tried in order. Different solvers fail on different problems, and cycling through
them is more robust than picking one - a portfolio optimisation that solves in research
and fails in production at 6am is a real operational problem."""


@dataclass
class OptimisationResult:
    weights: pd.Series
    status: str
    objective_value: float
    solver: str
    solve_time: float
    n_active: int
    turnover: float
    tracking_error: float | None = None
    binding_constraints: list[str] = field(default_factory=list)
    duals: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status in {"optimal", "optimal_inaccurate"}

    def summary(self) -> dict[str, object]:
        return {
            "status": self.status, "solver": self.solver,
            "objective": self.objective_value, "n_holdings": self.n_active,
            "turnover": self.turnover, "tracking_error": self.tracking_error,
            "max_weight": float(self.weights.max()) if len(self.weights) else 0.0,
            "binding": ", ".join(self.binding_constraints),
            "solve_time_s": self.solve_time,
        }


@dataclass
class Optimiser:
    """Builds and solves a constrained portfolio problem."""

    objective: Objective
    constraints: list[Constraint] = field(default_factory=list)
    costs: TransactionCostPenalty | None = None
    solver_order: tuple[str, ...] = SOLVERS
    verbose: bool = False

    def solve(self, data: ProblemData) -> OptimisationResult:
        import time

        n = len(data.securities)
        w = cp.Variable(n, name="w")

        expression = self.objective.build(w, data)
        if self.costs is not None:
            expression = expression + self.costs.build(w, data)

        built: list[cp.Constraint] = []
        owners: list[str] = []
        for constraint in self.constraints:
            parts = constraint.build(w, data)
            built.extend(parts)
            owners.extend([constraint.name] * len(parts))

        problem = cp.Problem(cp.Minimize(expression), built)

        last_error = ""
        for solver in self.solver_order:
            if solver not in cp.installed_solvers():
                continue
            start = time.perf_counter()
            try:
                problem.solve(solver=solver, verbose=self.verbose)
            except (cp.SolverError, ValueError) as exc:
                last_error = f"{solver}: {exc}"
                continue
            elapsed = time.perf_counter() - start
            if problem.status in {"optimal", "optimal_inaccurate"}:
                return self._package(problem, w, data, solver, elapsed, built, owners)
            last_error = f"{solver}: {problem.status}"

        return OptimisationResult(
            weights=pd.Series(0.0, index=data.securities),
            status=problem.status or "failed", objective_value=float("nan"),
            solver="none", solve_time=0.0, n_active=0, turnover=0.0,
            notes=[f"no solver succeeded ({last_error})"],
        )

    def _package(
        self,
        problem: cp.Problem,
        w: cp.Variable,
        data: ProblemData,
        solver: str,
        elapsed: float,
        built: list[cp.Constraint],
        owners: list[str],
    ) -> OptimisationResult:
        raw = np.asarray(w.value).flatten()
        # Solvers return small negatives on long-only problems and weights that sum to
        # 1 +/- 1e-9. Both are numerically fine and both break downstream code that
        # asserts an exact simplex, so they are cleaned here rather than everywhere else.
        raw = np.where(np.abs(raw) < 1e-9, 0.0, raw)
        total = raw.sum()
        if total > 0:
            raw = raw / total
        weights = pd.Series(raw, index=data.securities)

        w0 = data.align(data.initial_weights) if data.initial_weights is not None else None
        turnover = float(np.abs(raw - w0).sum() / 2) if w0 is not None else 0.0

        te: float | None = None
        try:
            active = weights - data.benchmark.reindex(data.securities).fillna(0.0)
            te = float(np.sqrt(max(_variance(active, data), 0.0)) * np.sqrt(252))
        except OptimisationError:
            te = None

        binding: list[str] = []
        duals: dict[str, float] = {}
        for name, constraint in zip(owners, built, strict=False):
            dual = getattr(constraint, "dual_value", None)
            if dual is None:
                continue
            magnitude = float(np.max(np.abs(np.atleast_1d(dual))))
            duals[name] = max(duals.get(name, 0.0), magnitude)
            # A non-zero dual means the constraint is binding: relaxing it would improve
            # the objective, and the dual is the exchange rate.
            if magnitude > 1e-7 and name not in binding:
                binding.append(name)

        return OptimisationResult(
            weights=weights, status=problem.status,
            objective_value=float(problem.value), solver=solver, solve_time=elapsed,
            n_active=int((raw > 1e-6).sum()), turnover=turnover, tracking_error=te,
            binding_constraints=binding, duals=duals,
        )


def _variance(weights: pd.Series, data: ProblemData) -> float:
    if data.factor_exposures is not None and data.factor_covariance is not None:
        b = data.factor_exposures.reindex(data.securities).fillna(0.0).to_numpy()
        f = data.factor_covariance.to_numpy()
        d = data.align(data.specific_variance, 1e-6)
        wv = weights.reindex(data.securities).fillna(0.0).to_numpy()
        x = b.T @ wv
        return float(x @ f @ x + (wv**2 * d).sum())
    if data.covariance is not None:
        sigma = data.covariance.reindex(
            index=data.securities, columns=data.securities).fillna(0.0).to_numpy()
        wv = weights.reindex(data.securities).fillna(0.0).to_numpy()
        return float(wv @ sigma @ wv)
    raise OptimisationError("no risk model supplied")


# --------------------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------------------


@dataclass
class InfeasibilityReport:
    feasible: bool
    culprits: list[str]
    """Minimal set of constraints whose removal makes the problem solvable."""

    conflicting_pairs: list[tuple[str, str]]
    relaxation_needed: dict[str, float]
    """Per constraint, the multiplicative relaxation that restores feasibility."""

    narrative: str

    def explain(self) -> str:
        return self.narrative


def diagnose_infeasible(
    objective: Objective,
    constraints: list[Constraint],
    data: ProblemData,
    max_relaxation: float = 8.0,
) -> InfeasibilityReport:
    """Work out which constraints are responsible for an infeasible problem.

    Three passes, cheapest first:

    1. Drop each soft constraint in turn. If removing one fixes it, that constraint
       alone is the culprit.
    2. Drop pairs. Most real infeasibilities are a conflict between two constraints -
       a turnover budget and a tracking-error cap, or a sector limit and a
       concentration limit - and neither is at fault alone.
    3. For each culprit, binary-search the relaxation factor that restores feasibility,
       so the report says *how much* rather than just *which*.
    """
    baseline = Optimiser(objective, constraints).solve(data)
    if baseline.succeeded:
        return InfeasibilityReport(True, [], [], {}, "The problem is feasible.")

    soft = [c for c in constraints if not c.is_hard]
    hard = [c for c in constraints if c.is_hard]

    if not soft:
        return InfeasibilityReport(
            False, [], [], {},
            "Infeasible with only hard constraints present. The universe cannot "
            "support full investment under the long-only requirement - check that the "
            "candidate set is non-empty and that benchmark weights are sane.",
        )

    culprits: list[str] = []
    for candidate in soft:
        subset = hard + [c for c in soft if c is not candidate]
        if Optimiser(objective, subset).solve(data).succeeded:
            culprits.append(candidate.name)

    pairs: list[tuple[str, str]] = []
    if not culprits:
        for a, b in itertools.combinations(soft, 2):
            subset = hard + [c for c in soft if c is not a and c is not b]
            if Optimiser(objective, subset).solve(data).succeeded:
                pairs.append((a.name, b.name))
                break

    relaxations: dict[str, float] = {}
    targets = [c for c in soft if c.name in culprits] or [
        c for c in soft if pairs and c.name in pairs[0]
    ]
    for constraint in targets:
        lo, hi = 1.0, max_relaxation
        best = float("inf")
        for _ in range(14):
            mid = (lo + hi) / 2
            trial = [c if c is not constraint else c.relaxed(mid) for c in constraints]
            if Optimiser(objective, trial).solve(data).succeeded:
                best, hi = mid, mid
            else:
                lo = mid
            if hi - lo < 0.01:
                break
        if np.isfinite(best):
            relaxations[constraint.name] = best

    return InfeasibilityReport(
        False, culprits, pairs, relaxations,
        _narrate(baseline.status, culprits, pairs, relaxations, constraints),
    )


def _narrate(
    status: str,
    culprits: list[str],
    pairs: list[tuple[str, str]],
    relaxations: dict[str, float],
    constraints: list[Constraint],
) -> str:
    lookup = {c.name: c for c in constraints}
    lines = [f"The problem is {status}."]
    if culprits:
        lines.append(
            "Removing any one of these constraints makes it solvable, so each is "
            "individually responsible:"
        )
        lines += [f"  - {n}: {lookup[n].describe()}" for n in culprits if n in lookup]
    elif pairs:
        a, b = pairs[0]
        lines.append(
            f"No single constraint is at fault. {a} and {b} conflict as a pair: "
            f"{lookup[a].describe() if a in lookup else a} "
            f"cannot hold at the same time as "
            f"{lookup[b].describe() if b in lookup else b}"
        )
    else:
        lines.append(
            "No single constraint or pair explains it, which points at a data problem "
            "rather than a specification problem - an empty candidate universe, a "
            "benchmark that does not sum to one, or a non-PSD covariance matrix."
        )
    for name, factor in relaxations.items():
        lines.append(
            f"  Relaxing {name} by a factor of {factor:.2f} restores feasibility."
        )
    return "\n".join(lines)


def price_constraints(
    objective: Objective,
    constraints: list[Constraint],
    data: ProblemData,
    relaxation: float = 1.5,
) -> pd.DataFrame:
    """What each constraint costs, in basis points of tracking error.

    Solve once with everything on, then once per soft constraint with that one relaxed,
    and report the improvement. This is the analysis behind every honest answer to
    "why can't you get me more factor exposure?" - and it turns a methodology argument
    into a table.
    """
    base = Optimiser(objective, constraints).solve(data)
    if not base.succeeded:
        raise OptimisationError(f"base problem is not solvable: {base.status}")

    rows = [{
        "constraint": "(all constraints)",
        "tracking_error": base.tracking_error,
        "objective": base.objective_value,
        "turnover": base.turnover,
        "te_saving_bps": 0.0,
        "binding": ", ".join(base.binding_constraints),
    }]

    for constraint in constraints:
        if constraint.is_hard:
            continue
        trial = [c if c is not constraint else c.relaxed(relaxation) for c in constraints]
        result = Optimiser(objective, trial).solve(data)
        if not result.succeeded:
            continue
        saving = (
            (base.tracking_error - result.tracking_error) * 10_000
            if base.tracking_error is not None and result.tracking_error is not None
            else float("nan")
        )
        rows.append({
            "constraint": f"{constraint.name} relaxed {relaxation:.1f}x",
            "tracking_error": result.tracking_error,
            "objective": result.objective_value,
            "turnover": result.turnover,
            "te_saving_bps": saving,
            "binding": ", ".join(result.binding_constraints),
        })

    return pd.DataFrame(rows).sort_values("te_saving_bps", ascending=False).reset_index(
        drop=True)


def select_top_n(
    objective: Objective,
    constraints: list[Constraint],
    data: ProblemData,
    n_holdings: int,
    max_rounds: int = 6,
) -> OptimisationResult:
    """Approximate a cardinality constraint by iterative trimming.

    Holding exactly N names is a non-convex constraint and would need a mixed-integer
    solver, which does not scale to a thousand-name universe. The standard heuristic:
    solve, drop the smallest positions, re-solve on the survivors, repeat.

    Documented as a heuristic rather than presented as an optimum, because it is one -
    it can miss the true optimal N-name portfolio, and a methodology that pretends
    otherwise is misleading.
    """
    result = Optimiser(objective, constraints).solve(data)

    for _ in range(max_rounds):
        held = result.weights[result.weights > 1e-6]
        if len(held) <= n_holdings:
            break
        keep = list(held.nlargest(n_holdings).index)
        trimmed = ProblemData(
            securities=keep,
            benchmark=data.benchmark.reindex(keep).fillna(0.0),
            initial_weights=(data.initial_weights.reindex(keep).fillna(0.0)
                             if data.initial_weights is not None else None),
            expected_returns=(data.expected_returns.reindex(keep)
                              if data.expected_returns is not None else None),
            covariance=(data.covariance.reindex(index=keep, columns=keep)
                        if data.covariance is not None else None),
            factor_exposures=(data.factor_exposures.reindex(keep)
                              if data.factor_exposures is not None else None),
            factor_covariance=data.factor_covariance,
            specific_variance=(data.specific_variance.reindex(keep)
                               if data.specific_variance is not None else None),
            adv=data.adv.reindex(keep) if data.adv is not None else None,
            attributes=data.attributes.reindex(keep) if not data.attributes.empty
            else data.attributes,
        )
        result = Optimiser(objective, constraints).solve(trimmed)
        if not result.succeeded:
            break

    result.notes.append(
        f"cardinality of {n_holdings} approximated by iterative trimming from "
        f"{len(data.securities)} candidates; this is a heuristic, not a proven optimum"
    )
    return result
