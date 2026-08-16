"""Reconciling a rebuilt index against an independently published one.

The question this answers is the only one that matters about an index implementation:
**did we build what the rules say?** Property tests prove the engine is internally
consistent; only a reconciliation against an outside source proves it is *right*.

The discipline, which is what makes this different from plotting two lines and saying
they look close:

1. Reconcile at the **constituent** level first, not the return level. Two indices can
   have matching returns for a week and completely different holdings.
2. Attribute every basis point of difference to a **named cause**.
3. Report what is left as an unexplained residual, and treat its size as the measure of
   how well the difference is understood. A reconciliation whose residual is larger than
   its explained components has explained nothing.

`reconcile_against_published` works against any source of external weights: an iShares
holdings file, an LSEG constituent request, or a second implementation.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class WeightDifference:
    security_id: str
    our_weight: float
    their_weight: float
    difference: float
    category: str
    """held_by_us_only | held_by_them_only | weight_difference"""

    @property
    def abs_difference(self) -> float:
        return abs(self.difference)


@dataclass
class ConstituentReconciliation:
    as_of: dt.date
    n_ours: int
    n_theirs: int
    n_common: int
    differences: list[WeightDifference]
    total_absolute_difference: float
    """Sum of absolute weight differences. Twice the active share against a portfolio
    that is meant to be identical."""

    @property
    def matched_weight(self) -> float:
        return 1.0 - self.total_absolute_difference / 2.0

    @property
    def only_ours(self) -> list[WeightDifference]:
        return [d for d in self.differences if d.category == "held_by_us_only"]

    @property
    def only_theirs(self) -> list[WeightDifference]:
        return [d for d in self.differences if d.category == "held_by_them_only"]

    def worst(self, n: int = 10) -> list[WeightDifference]:
        return sorted(self.differences, key=lambda d: -d.abs_difference)[:n]

    def summary(self) -> dict[str, Any]:
        return {
            "as_of": str(self.as_of),
            "constituents_ours": self.n_ours,
            "constituents_theirs": self.n_theirs,
            "common": self.n_common,
            "only_ours": len(self.only_ours),
            "only_theirs": len(self.only_theirs),
            "matched_weight": self.matched_weight,
            "total_absolute_weight_difference": self.total_absolute_difference,
        }

    def verdict(self) -> str:
        if self.matched_weight > 0.995 and not self.only_ours and not self.only_theirs:
            return "Membership and weights match to within 50bp. The rules agree."
        if self.matched_weight > 0.97:
            return (
                f"Membership matches but weights differ by "
                f"{self.total_absolute_difference:.2%} in aggregate. Usually a "
                "free-float vintage difference rather than a rules difference - float "
                "factors are revised between reviews and the two sides are unlikely to "
                "be using the same snapshot."
            )
        return (
            f"Material disagreement: only {self.matched_weight:.1%} of weight is "
            f"matched, with {len(self.only_ours)} names we hold that they do not and "
            f"{len(self.only_theirs)} the other way. This is a rules difference, not a "
            "data vintage difference, and it needs to be traced to a specific screen."
        )


def reconcile_constituents(
    ours: pd.Series, theirs: pd.Series, as_of: dt.date, tolerance: float = 1e-6
) -> ConstituentReconciliation:
    """Compare two weight vectors security by security.

    Both are renormalised first. A published holdings file usually carries a small cash
    line and rounds weights to a few decimals, so the raw sums differ by a few basis
    points; treating that as a real difference buries the findings that matter.
    """
    ours = ours[ours > 0]
    theirs = theirs[theirs > 0]
    ours = ours / ours.sum() if ours.sum() > 0 else ours
    theirs = theirs / theirs.sum() if theirs.sum() > 0 else theirs

    keys = sorted(set(ours.index) | set(theirs.index))
    differences: list[WeightDifference] = []
    total = 0.0
    for key in keys:
        our_weight = float(ours.get(key, 0.0))
        their_weight = float(theirs.get(key, 0.0))
        diff = our_weight - their_weight
        if abs(diff) <= tolerance:
            continue
        if their_weight == 0:
            category = "held_by_us_only"
        elif our_weight == 0:
            category = "held_by_them_only"
        else:
            category = "weight_difference"
        differences.append(WeightDifference(key, our_weight, their_weight, diff, category))
        total += abs(diff)

    return ConstituentReconciliation(
        as_of=as_of,
        n_ours=len(ours),
        n_theirs=len(theirs),
        n_common=len(set(ours.index) & set(theirs.index)),
        differences=differences,
        total_absolute_difference=total,
    )


# --------------------------------------------------------------------------------------


@dataclass
class ReturnReconciliation:
    """Return difference decomposed into named causes."""

    period_start: dt.date
    period_end: dt.date
    our_return: float
    their_return: float
    components: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_difference(self) -> float:
        return self.our_return - self.their_return

    @property
    def explained(self) -> float:
        return float(sum(c["impact"] for c in self.components))

    @property
    def unexplained(self) -> float:
        return self.total_difference - self.explained

    @property
    def explained_share(self) -> float:
        return (
            abs(self.explained) / abs(self.total_difference)
            if abs(self.total_difference) > 1e-12
            else 1.0
        )

    def add(self, name: str, impact: float, note: str) -> None:
        self.components.append({"component": name, "impact": impact, "note": note})

    def to_frame(self) -> pd.DataFrame:
        rows = [
            {
                "component": "their return",
                "impact": self.their_return,
                "cumulative": self.their_return,
                "note": "the published series",
            }
        ]
        running = self.their_return
        for component in self.components:
            running += component["impact"]
            rows.append({**component, "cumulative": running})
        rows.append(
            {
                "component": "unexplained residual",
                "impact": self.unexplained,
                "cumulative": running + self.unexplained,
                "note": "not attributable to an identified cause",
            }
        )
        rows.append(
            {
                "component": "our return",
                "impact": 0.0,
                "cumulative": self.our_return,
                "note": "the rebuilt series",
            }
        )
        return pd.DataFrame(rows)

    def verdict(self) -> str:
        if abs(self.total_difference) < 0.0005:
            return (
                f"The two series differ by {self.total_difference * 10_000:.1f}bp over "
                "the period, which is within the rounding of a published holdings file. "
                "No further explanation is required."
            )
        if self.explained_share > 0.8:
            return (
                f"Of a {self.total_difference * 10_000:.0f}bp difference, "
                f"{self.explained_share:.0%} is attributed to identified causes and "
                f"{self.unexplained * 10_000:.0f}bp is residual. That is a "
                "reconciliation."
            )
        return (
            f"Only {self.explained_share:.0%} of a "
            f"{self.total_difference * 10_000:.0f}bp difference is explained. The "
            f"residual of {self.unexplained * 10_000:.0f}bp is larger than the "
            "components, which means the difference is not understood - and an "
            "unexplained residual is not evidence of a small error, it is absence of "
            "evidence either way."
        )


def reconcile_returns(
    our_levels: pd.Series,
    their_levels: pd.Series,
    constituent_diff: ConstituentReconciliation | None = None,
    fee_bps: float = 0.0,
    withholding_difference: float = 0.0,
) -> ReturnReconciliation:
    """Explain the return difference between a rebuilt index and a published one.

    The standard causes, in the order they usually matter when reconciling against an
    ETF NAV rather than an index level:

    * **fees** - an ETF charges them, an index does not. Almost always the largest term
      and the easiest to forget, because it is invisible in the index documentation.
    * **withholding tax** - a fund's actual reclaim experience differs from the notional
      non-resident investor the net index assumes.
    * **membership** - names held by one side and not the other.
    * **cash drag and sampling** - a fund holding 95% of the index by weight.
    """
    common = our_levels.index.intersection(their_levels.index)
    if len(common) < 2:
        raise ValueError("need at least two overlapping dates to reconcile returns")
    ours = our_levels.loc[common]
    theirs = their_levels.loc[common]

    start, end = common[0], common[-1]
    years = max((end - start).days / 365.25, 1e-9)
    our_return = float(ours.iloc[-1] / ours.iloc[0] - 1.0)
    their_return = float(theirs.iloc[-1] / theirs.iloc[0] - 1.0)

    reconciliation = ReturnReconciliation(
        period_start=start,
        period_end=end,
        our_return=our_return,
        their_return=their_return,
    )

    if fee_bps:
        impact = fee_bps / 10_000 * years
        reconciliation.add(
            "management fee",
            impact,
            f"{fee_bps:.0f}bp a year over {years:.2f} years. The index bears no fee; "
            "the fund does, so the fund must lag by at least this much.",
        )
    if withholding_difference:
        reconciliation.add(
            "withholding tax treatment",
            withholding_difference,
            "difference between the fund's actual reclaim experience and the notional "
            "non-resident investor the net index assumes",
        )
    if constituent_diff is not None and constituent_diff.differences:
        # Membership differences are attributed at their weight, which is an upper
        # bound rather than a measurement: it assumes the mismatched names moved with
        # the index. Stated as such, because an attribution that overstates its own
        # precision is worse than a wider bound honestly labelled.
        weight_gap = constituent_diff.total_absolute_difference / 2
        reconciliation.add(
            "membership and weight differences",
            0.0,
            f"{weight_gap:.2%} of weight is allocated differently across "
            f"{len(constituent_diff.differences)} securities. Upper bound on the return "
            "impact, not a measurement.",
        )
    return reconciliation


# --------------------------------------------------------------------------------------


def reconcile_against_published(
    our_weights: pd.Series,
    their_weights: pd.Series,
    our_levels: pd.Series | None = None,
    their_levels: pd.Series | None = None,
    as_of: dt.date | None = None,
    fee_bps: float = 0.0,
) -> dict[str, Any]:
    """The full study: constituents first, then returns, then a verdict."""
    as_of = as_of or dt.date.today()
    constituents = reconcile_constituents(our_weights, their_weights, as_of)
    out: dict[str, Any] = {
        "constituents": constituents,
        "constituent_summary": constituents.summary(),
        "constituent_verdict": constituents.verdict(),
    }
    if our_levels is not None and their_levels is not None:
        returns = reconcile_returns(our_levels, their_levels, constituents, fee_bps=fee_bps)
        out["returns"] = returns
        out["return_verdict"] = returns.verdict()
    return out


def self_reconciliation(
    history_a: Any, history_b: Any, label_a: str = "run A", label_b: str = "run B"
) -> dict[str, Any]:
    """Reconcile two runs of our own engine against each other.

    The reconciliation that can actually be run without a licensed data feed, and it is
    not a toy: comparing a full historical rebuild against the incremental daily job is
    exactly how a production index provider detects that its overnight process has
    drifted from its own published methodology.

    A difference here is unambiguously a bug, because both sides used identical inputs.
    """
    a = history_a.levels.set_index("date")["gross_total_return"]
    b = history_b.levels.set_index("date")["gross_total_return"]
    common = a.index.intersection(b.index)
    if len(common) == 0:
        return {"overlapping_dates": 0, "identical": False, "note": "the two runs share no dates"}

    diff = (a.loc[common] - b.loc[common]).abs()
    relative = diff / b.loc[common].abs().clip(lower=1e-12)
    worst_date = relative.idxmax()

    return {
        "label_a": label_a,
        "label_b": label_b,
        "overlapping_dates": len(common),
        "max_absolute_difference": float(diff.max()),
        "max_relative_difference_bps": float(relative.max() * 10_000),
        "worst_date": str(worst_date),
        "identical": bool(relative.max() < 1e-9),
        "note": (
            "Identical to floating-point precision."
            if relative.max() < 1e-9
            else f"Diverges by up to {relative.max() * 10_000:.2f}bp, worst on "
            f"{worst_date}. Both runs used the same inputs, so this is a bug "
            "rather than a data difference."
        ),
    }


def write_reconciliation_study(result: dict[str, Any], title: str, out: Any) -> Any:
    """Render the study as a markdown document."""
    from pathlib import Path

    path = Path(out)
    constituents: ConstituentReconciliation = result["constituents"]
    lines = [
        f"# Reconciliation study — {title}",
        "",
        f"**As at** {constituents.as_of}",
        "",
        "---",
        "",
        "## 1. Constituents",
        "",
        "Reconciled at the holding level first. Two indices can post matching returns "
        "for a week while holding entirely different securities, so a return comparison "
        "alone proves nothing.",
        "",
        "| Measure | Value |",
        "|---|---:|",
    ]
    for key, value in constituents.summary().items():
        formatted = f"{value:.4%}" if isinstance(value, float) else str(value)
        lines.append(f"| {key.replace('_', ' ')} | {formatted} |")

    lines += ["", f"**{constituents.verdict()}**", ""]

    if constituents.differences:
        lines += [
            "### Largest differences",
            "",
            "| Security | Ours | Theirs | Difference | Category |",
            "|---|---:|---:|---:|---|",
        ]
        lines += [
            f"| {d.security_id} | {d.our_weight:.4%} | {d.their_weight:.4%} | "
            f"{d.difference:+.4%} | {d.category.replace('_', ' ')} |"
            for d in constituents.worst(10)
        ]
        lines.append("")

    if "returns" in result:
        returns: ReturnReconciliation = result["returns"]
        lines += [
            "## 2. Returns",
            "",
            f"Period {returns.period_start} to {returns.period_end}.",
            "",
            "| Component | Impact | Running | Note |",
            "|---|---:|---:|---|",
        ]
        for row in returns.to_frame().itertuples(index=False):
            lines.append(
                f"| {row.component} | {row.impact:+.4%} | {row.cumulative:.4%} | {row.note} |"
            )
        lines += ["", f"**{returns.verdict()}**", ""]

    lines += [
        "## 3. What an unexplained residual means",
        "",
        "The residual is the honest measure of how well the difference is understood, "
        "and it is reported rather than absorbed into the nearest named line. Pushing "
        "it into 'transaction costs' would make the table sum correctly and the "
        "reconciliation worthless.",
        "",
        "---",
        "",
        "*Reconciled against a simulated reference. Not an investable benchmark.*",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def synthetic_published_index(
    our_weights: pd.Series,
    seed: int = 7,
    drop_fraction: float = 0.02,
    float_noise: float = 0.01,
) -> pd.Series:
    """Construct a plausible 'published' comparison from our own weights.

    Stands in for a licensed constituent file so the reconciliation machinery is
    genuinely exercised. The perturbations mimic the two differences that actually show
    up against a real published index: a handful of names excluded by a screen applied
    slightly differently, and free-float factors from a different vintage.

    Clearly labelled as synthetic. It demonstrates that the reconciliation *works*; it
    does not demonstrate that our rules match anyone's.
    """
    rng = np.random.default_rng(seed)
    weights = our_weights.copy()
    n_drop = max(1, int(len(weights) * drop_fraction))
    dropped = rng.choice(weights.index, size=n_drop, replace=False)
    weights = weights.drop(index=dropped)
    weights = weights * (1 + rng.normal(0, float_noise, size=len(weights)))
    weights = weights.clip(lower=0)
    return weights / weights.sum()
