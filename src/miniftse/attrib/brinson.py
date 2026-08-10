"""Performance attribution: Brinson-Fachler, factor-based, and index-specific.

Half of client enquiries are "why did the index do X?", and the answer has to reconcile
exactly to the active return. A decomposition whose components do not add up is worse
than none, because the client will add them up.

Three views, answering different questions:

* **Brinson-Fachler** - allocation versus selection, by sector. What a traditional
  equity client expects.
* **Factor-based** - return explained by style exposures, using the risk model. What a
  quantitative client expects.
* **Index-specific** - additions, deletions, capping, currency, review effects. What
  nobody else computes and what an *index provider* actually gets asked about.

The three do not agree, and that is fine as long as you can say why. Brinson attributes
to sectors, the factor model to styles, and a stock that is both "technology" and "high
growth" gets counted in different buckets by each.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class BrinsonResult:
    by_group: pd.DataFrame
    total_allocation: float
    total_selection: float
    total_interaction: float
    total_active: float
    portfolio_return: float
    benchmark_return: float

    @property
    def reconciles(self) -> bool:
        """Components must sum to active return. The acceptance test."""
        parts = self.total_allocation + self.total_selection + self.total_interaction
        return abs(parts - self.total_active) < 1e-9

    def summary(self) -> str:
        return (
            f"Active return {self.total_active:+.2%} = "
            f"allocation {self.total_allocation:+.2%} + "
            f"selection {self.total_selection:+.2%} + "
            f"interaction {self.total_interaction:+.2%}"
            + ("" if self.reconciles else "  [DOES NOT RECONCILE]")
        )


def brinson_fachler(
    portfolio_weights: pd.Series,
    benchmark_weights: pd.Series,
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    groups: pd.Series,
) -> BrinsonResult:
    """Brinson-Fachler attribution by group.

    For each group ``g``::

        allocation_g  = (w_p,g - w_b,g) * (r_b,g - r_b_total)
        selection_g   = w_b,g * (r_p,g - r_b,g)
        interaction_g = (w_p,g - w_b,g) * (r_p,g - r_b,g)

    Fachler rather than Hood-Brinson: allocation is measured against the *total*
    benchmark return, not against zero. It matters. Overweighting a sector that returned
    +5% when the market returned +8% is a bad allocation decision, and the original
    Brinson formulation scores it as a good one.

    The honest limitation: this assumes weights held constant through the period. Over a
    quarter with a reconstitution in the middle, it is an approximation, and the
    index-specific attribution below is the better tool for that case.
    """
    idx = groups.dropna().index
    pw = portfolio_weights.reindex(idx).fillna(0.0)
    bw = benchmark_weights.reindex(idx).fillna(0.0)
    pr = portfolio_returns.reindex(idx).fillna(0.0)
    br = benchmark_returns.reindex(idx).fillna(0.0)
    g = groups.reindex(idx)

    total_benchmark = float((bw * br).sum() / bw.sum()) if bw.sum() > 0 else 0.0

    rows = []
    for group in g.dropna().unique():
        mask = g == group
        wp, wb = float(pw[mask].sum()), float(bw[mask].sum())
        rp = float((pw[mask] * pr[mask]).sum() / wp) if wp > 0 else 0.0
        rb = float((bw[mask] * br[mask]).sum() / wb) if wb > 0 else 0.0

        allocation = (wp - wb) * (rb - total_benchmark)
        selection = wb * (rp - rb)
        interaction = (wp - wb) * (rp - rb)

        rows.append({
            "group": str(group), "portfolio_weight": wp, "benchmark_weight": wb,
            "active_weight": wp - wb, "portfolio_return": rp, "benchmark_return": rb,
            "allocation": allocation, "selection": selection,
            "interaction": interaction, "total": allocation + selection + interaction,
        })

    frame = pd.DataFrame(rows).sort_values("total", ascending=False).reset_index(drop=True)
    portfolio_total = float((pw * pr).sum() / pw.sum()) if pw.sum() > 0 else 0.0

    return BrinsonResult(
        by_group=frame,
        total_allocation=float(frame["allocation"].sum()),
        total_selection=float(frame["selection"].sum()),
        total_interaction=float(frame["interaction"].sum()),
        total_active=portfolio_total - total_benchmark,
        portfolio_return=portfolio_total,
        benchmark_return=total_benchmark,
    )


# --------------------------------------------------------------------------------------


@dataclass
class FactorAttribution:
    by_factor: pd.DataFrame
    specific_return: float
    total_active: float
    explained_fraction: float

    def summary(self) -> str:
        explained = self.by_factor["contribution"].sum()
        return (
            f"Active return {self.total_active:+.2%} = "
            f"factors {explained:+.2%} + specific {self.specific_return:+.2%}. "
            f"The model explains {self.explained_fraction:.0%} of the active return."
        )


def factor_attribution(
    active_weights: pd.Series,
    exposures: pd.DataFrame,
    factor_returns: pd.Series,
    asset_returns: pd.Series,
) -> FactorAttribution:
    """Decompose active return into factor contributions plus specific.

    Contribution of factor ``k`` is ``(B'w_active)_k * f_k``. What is left over is
    specific return - stock picking, in a portfolio; in an index it is mostly the
    residual of the weighting scheme, and a large specific component usually means the
    risk model is missing a factor the index is actually taking.
    """
    ids = exposures.index
    w = active_weights.reindex(ids).fillna(0.0)
    x = exposures.T @ w
    f = factor_returns.reindex(x.index).fillna(0.0)
    contributions = x * f

    total_active = float((w * asset_returns.reindex(ids).fillna(0.0)).sum())
    explained = float(contributions.sum())
    specific = total_active - explained

    frame = pd.DataFrame({
        "factor": x.index, "exposure": x.to_numpy(),
        "factor_return": f.to_numpy(), "contribution": contributions.to_numpy(),
    }).sort_values("contribution", key=np.abs, ascending=False).reset_index(drop=True)

    return FactorAttribution(
        by_factor=frame, specific_return=specific, total_active=total_active,
        explained_fraction=(explained / total_active if total_active else 0.0),
    )


# --------------------------------------------------------------------------------------


@dataclass
class IndexAttribution:
    """Why a variant index differs from its parent. The index-provider-specific view."""

    components: pd.DataFrame
    total_active: float
    unexplained: float

    def summary(self) -> str:
        lines = [f"Active return versus parent: {self.total_active:+.2%}", ""]
        for row in self.components.itertuples(index=False):
            lines.append(f"  {row.component:<24} {row.contribution:+.3%}  {row.note}")
        lines.append(f"  {'unexplained residual':<24} {self.unexplained:+.3%}")
        return "\n".join(lines)


def index_vs_parent(
    child_weights: pd.Series,
    parent_weights: pd.Series,
    returns: pd.Series,
    capping_factors: pd.Series | None = None,
    currency_returns: pd.Series | None = None,
    groups: pd.Series | None = None,
) -> IndexAttribution:
    """Attribute a child index's active return to the design choices that created it.

    The components are the decisions a methodology actually makes:

    * **selection** - the effect of holding a different set of names
    * **reweighting** - the effect of different weights on shared names
    * **capping** - the effect of the concentration constraint specifically
    * **currency** - the effect of differing currency composition

    This is the attribution a client wants when they ask why the factor index lagged the
    parent, and it maps onto things you can change - unlike a Brinson table, which maps
    onto sectors nobody chose directly.
    """
    ids = returns.index
    cw = child_weights.reindex(ids).fillna(0.0)
    pw = parent_weights.reindex(ids).fillna(0.0)
    r = returns.reindex(ids).fillna(0.0)

    child_return = float((cw * r).sum())
    parent_return = float((pw * r).sum())
    total_active = child_return - parent_return

    held_by_both = (cw > 0) & (pw > 0)
    only_child = (cw > 0) & (pw <= 0)
    only_parent = (cw <= 0) & (pw > 0)

    rows = [
        {
            "component": "names held only by child",
            "contribution": float((cw[only_child] * r[only_child]).sum()),
            "note": f"{int(only_child.sum())} names, "
                    f"{float(cw[only_child].sum()):.1%} weight",
        },
        {
            "component": "names dropped from parent",
            "contribution": -float((pw[only_parent] * r[only_parent]).sum()),
            "note": f"{int(only_parent.sum())} names, "
                    f"{float(pw[only_parent].sum()):.1%} weight",
        },
        {
            "component": "reweighting of shared names",
            "contribution": float(
                ((cw[held_by_both] - pw[held_by_both]) * r[held_by_both]).sum()
            ),
            "note": f"{int(held_by_both.sum())} names in both",
        },
    ]

    if capping_factors is not None:
        cf = capping_factors.reindex(ids).fillna(1.0)
        uncapped = cw / cf.where(cf > 0, 1.0)
        uncapped = uncapped / uncapped.sum() if uncapped.sum() > 0 else uncapped
        rows.append({
            "component": "capping",
            "contribution": float(((cw - uncapped) * r).sum()),
            "note": f"{int((cf < 0.999).sum())} names capped",
        })

    if currency_returns is not None:
        fx = currency_returns.reindex(ids).fillna(0.0)
        rows.append({
            "component": "currency composition",
            "contribution": float(((cw - pw) * fx).sum()),
            "note": "differing currency weights",
        })

    if groups is not None:
        g = groups.reindex(ids)
        largest = (
            pd.DataFrame({"g": g, "active": cw - pw, "r": r})
            .dropna().groupby("g")
            .apply(lambda d: float((d["active"] * d["r"]).sum()), include_groups=False)
        )
        if not largest.empty:
            top = largest.abs().idxmax()
            rows.append({
                "component": f"largest group effect ({top})",
                "contribution": 0.0,
                "note": f"{float(largest[top]):+.3%} from {top}; memo item, already "
                        "counted in reweighting",
            })

    frame = pd.DataFrame(rows)
    counted = float(frame[frame["contribution"] != 0]["contribution"].sum())
    return IndexAttribution(
        components=frame, total_active=total_active,
        unexplained=total_active - counted,
    )


# --------------------------------------------------------------------------------------


@dataclass
class BacktestLiveBridge:
    """Line-by-line reconciliation from a pro-forma backtest to a live index.

    The hardest client question there is: "your backtest showed a Sharpe of 0.9 and the
    live index has done 0.4 - was the backtest wrong?"

    The honest answer is usually "no, but it was measuring something a live index cannot
    achieve", and the only credible form that answer can take is this table. Each line
    is a specific, quantified difference. Anything left in the residual is the part you
    genuinely cannot explain, and reporting it rather than burying it is the point.
    """

    rows: list[dict[str, object]] = field(default_factory=list)

    def add(self, name: str, impact: float, explanation: str) -> "BacktestLiveBridge":
        self.rows.append({"item": name, "impact": impact, "explanation": explanation})
        return self

    def build(self, backtest_return: float, live_return: float) -> pd.DataFrame:
        explained = sum(float(r["impact"]) for r in self.rows)
        residual = (live_return - backtest_return) - explained
        frame = pd.DataFrame(
            [{"item": "backtest return", "impact": backtest_return,
              "explanation": "as published in the research paper"}]
            + self.rows
            + [{"item": "unexplained residual", "impact": residual,
                "explanation": "not attributable to any identified difference"},
               {"item": "live return", "impact": live_return,
                "explanation": "actual published index"}]
        )
        frame["cumulative"] = frame["impact"].cumsum()
        return frame


def standard_bridge_items() -> BacktestLiveBridge:
    """The differences that show up in nearly every backtest-to-live reconciliation.

    Impacts are zero placeholders - each has to be measured for the specific index.
    The value is the checklist: these are the five things that are always different,
    and forgetting to look at one is how a reconciliation ends up with a large residual.
    """
    return (
        BacktestLiveBridge()
        .add("data vintage", 0.0,
             "the backtest used data as it stands today, including restatements and "
             "vendor corrections that were not available at the time")
        .add("pro-forma reviews", 0.0,
             "backtest reviews were applied on the theoretical schedule; live reviews "
             "have announcement lags, committee exceptions and fast entries")
        .add("survivorship in the candidate universe", 0.0,
             "the backtest universe was built from securities that exist today unless "
             "explicitly corrected for")
        .add("transaction costs", 0.0,
             "an index does not itself trade, but a comparison against a tracking fund "
             "must include the fund's costs")
        .add("corporate action treatment", 0.0,
             "the backtest applied a simplified corporate action model; live treatment "
             "follows the full published rules including exceptions")
    )
