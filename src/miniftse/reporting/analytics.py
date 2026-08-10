"""Running the risk model and attribution against a real index, and reporting them.

Everything upstream of this is a component: `risk.factor_model` can estimate a model,
`attrib.brinson` can decompose a return. This module is what actually points them at a
built index and produces the two documents a client asks for — a risk one-pager and an
attribution one-pager.

The measurement discipline that matters here: **ex-ante risk must be estimated on data
available before the period it forecasts.** A risk report built from returns that include
the period being reported is not a forecast, and its bias test will look wonderful for
reasons that have nothing to do with the model.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from miniftse.attrib.brinson import (
    BacktestLiveBridge,
    brinson_fachler,
    factor_attribution,
    index_vs_parent,
)
from miniftse.factors.definitions import ALL_FACTORS
from miniftse.risk.covariance import bias_test
from miniftse.risk.factor_model import FactorModelEstimator, RiskModel, build_exposures

if TYPE_CHECKING:
    from miniftse.production.variants import VariantResult

STYLE_FACTORS = ("value", "quality", "momentum", "low_volatility", "size")


@dataclass
class RiskAnalysis:
    model: RiskModel
    portfolio_weights: pd.Series
    benchmark_weights: pd.Series
    ex_ante_te: float
    ex_post_te: float
    decomposition: pd.DataFrame
    marginal: pd.Series
    bias: dict[str, float]
    exposures: pd.Series
    as_of: dt.date

    @property
    def bias_verdict(self) -> str:
        ratio = self.bias.get("bias_statistic", float("nan"))
        if not np.isfinite(ratio):
            return "insufficient data for a bias test"
        if self.bias.get("within_ci"):
            return f"unbiased (statistic {ratio:.2f}, inside the confidence interval)"
        if ratio > 1:
            return (
                f"UNDER-forecasts risk (statistic {ratio:.2f}). Realised volatility "
                "exceeded the forecast, which is the dangerous direction: it shows up "
                "in a crisis, when the forecast is being relied on most."
            )
        return (
            f"OVER-forecasts risk (statistic {ratio:.2f}). Costs return through "
            "unnecessary caution, but does not surprise anyone."
        )

    def summary(self) -> dict[str, Any]:
        factor_share = float(
            self.decomposition.loc[
                self.decomposition["type"] == "factor", "variance_contribution"].sum()
        )
        total = float(self.decomposition["variance_contribution"].sum())
        return {
            "as_of": str(self.as_of),
            "ex_ante_tracking_error": self.ex_ante_te,
            "ex_post_tracking_error": self.ex_post_te,
            "forecast_ratio": (self.ex_post_te / self.ex_ante_te
                               if self.ex_ante_te else float("nan")),
            "factor_share_of_risk": factor_share / total if total else 0.0,
            "specific_share_of_risk": 1 - (factor_share / total if total else 0.0),
            "n_factors": len(self.model.factors),
            "bias_statistic": self.bias.get("bias_statistic", float("nan")),
            "bias_verdict": self.bias_verdict,
        }


def build_risk_model(
    variant: VariantResult,
    as_of: dt.date,
    estimation_days: int = 500,
    factors: tuple[str, ...] = STYLE_FACTORS,
) -> RiskModel:
    """Estimate a Barra-lite model from data ending at `as_of`.

    Exposures are the factor scores the index itself uses, so the risk model speaks the
    same language as the product — a value tilt's active risk decomposes onto the value
    factor rather than onto something adjacent. That alignment is the main practical
    argument for a fundamental model over a statistical one.
    """
    from miniftse.factors.build import FactorInputBuilder

    universe = variant.reconstitution
    prices = universe.prices
    wide = prices.pivot_table(index="date", columns="security_id", values="close",
                              aggfunc="last").sort_index()
    returns = wide.loc[wide.index <= as_of].tail(estimation_days).pct_change().iloc[1:]

    builder = FactorInputBuilder(provider=variant.score_provider.builder.provider
                                 if variant.score_provider else None)
    if variant.score_provider is not None:
        builder = variant.score_provider.builder

    # Exposures are held fixed at the estimation date rather than recomputed daily.
    # Rebuilding a full cross-section for every one of 500 days would dominate the cost
    # of the whole report, and style exposures move slowly enough that the approximation
    # is standard practice as well as pragmatic.
    inputs = builder.build(as_of, fx_rates=getattr(variant.score_provider, "fx_rates",
                                                   {}) or {})
    scores = pd.DataFrame({f: ALL_FACTORS[f].compute(inputs) for f in factors})
    exposures = build_exposures(scores, inputs.industry, include_market=True)

    common = [c for c in returns.columns if c in exposures.index]
    returns = returns[common]
    exposures = exposures.loc[common]

    weights = pd.DataFrame(
        {c: inputs.market_cap.get(c, 1.0) for c in common}, index=returns.index
    )
    estimator = FactorModelEstimator()
    return estimator.fit(
        returns=returns,
        exposures=dict.fromkeys(returns.index, exposures),
        weights=weights,
        as_of=as_of,
    )


def analyse_risk(
    variant: VariantResult,
    parent: VariantResult,
    as_of: dt.date | None = None,
    forward_days: int = 250,
) -> RiskAnalysis:
    """Full ex-ante risk analysis of a variant against its parent.

    The bias test compares the forecast made at `as_of` with what actually happened
    *afterwards*. Estimating on data that includes the forecast period would produce a
    bias statistic near 1.0 that means nothing.
    """
    levels = variant.history.levels
    dates = list(levels["date"])
    as_of = as_of or dates[max(0, len(dates) - forward_days - 1)]

    model = build_risk_model(variant, as_of)

    portfolio = _weights_at(variant, as_of)
    benchmark = _weights_at(parent, as_of)
    ids = model.exposures.index
    portfolio = portfolio.reindex(ids).fillna(0.0)
    benchmark = benchmark.reindex(ids).fillna(0.0)
    if portfolio.sum() > 0:
        portfolio = portfolio / portfolio.sum()
    if benchmark.sum() > 0:
        benchmark = benchmark / benchmark.sum()

    ex_ante = model.tracking_error(portfolio, benchmark)

    # Realised active return AFTER the estimation date.
    merged = (
        levels[["date", "gross_total_return"]]
        .merge(parent.history.levels[["date", "gross_total_return"]], on="date",
               suffixes=("", "_parent"))
    )
    forward = merged[merged["date"] > as_of].head(forward_days)
    active = (forward["gross_total_return"].pct_change()
              - forward["gross_total_return_parent"].pct_change()).dropna()
    ex_post = float(active.std() * np.sqrt(252)) if len(active) > 20 else float("nan")

    daily_forecast = pd.Series(
        model.tracking_error(portfolio, benchmark, annualise=False),
        index=active.index,
    )
    return RiskAnalysis(
        model=model, portfolio_weights=portfolio, benchmark_weights=benchmark,
        ex_ante_te=ex_ante, ex_post_te=ex_post,
        decomposition=model.risk_decomposition(portfolio, benchmark),
        marginal=model.marginal_contributions(portfolio, benchmark),
        bias=bias_test(daily_forecast, active), exposures=model.portfolio_exposures(
            portfolio - benchmark), as_of=as_of,
    )


def _weights_at(variant: VariantResult, as_of: dt.date) -> pd.Series:
    weights = variant.history.weights
    if weights.empty:
        return pd.Series(dtype=float)
    available = [d for d in weights["date"].unique() if d <= as_of]
    date = max(available) if available else weights["date"].min()
    return weights[weights["date"] == date].set_index("security_id")["weight"]


# --------------------------------------------------------------------------------------


@dataclass
class AttributionAnalysis:
    period_start: dt.date
    period_end: dt.date
    active_return: float
    brinson: Any
    index_effects: Any
    factor: Any | None = None
    notes: list[str] = field(default_factory=list)


def analyse_attribution(
    variant: VariantResult,
    parent: VariantResult,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> AttributionAnalysis:
    """Brinson plus index-specific attribution of a variant against its parent."""
    levels = variant.history.levels
    # Default to the LAST REVIEW PERIOD, not the whole history.
    #
    # Brinson assumes weights held constant through the period. Run over six years and
    # twenty-four reviews it reported +23.8% of active return against an actual +10.6% -
    # not a bug in the arithmetic, a misuse of the method. Over one review period the
    # constant-weight assumption approximately holds, and longer horizons are built by
    # linking single-period attributions rather than by widening the window.
    reviews = variant.history.reviews
    if start is None:
        if not reviews.empty:
            start = reviews["date"].iloc[-1]
        else:
            start = levels["date"].iloc[max(0, len(levels) - 63)]
    end = end or levels["date"].iloc[-1]

    child_weights = _weights_at(variant, start)
    parent_weights = _weights_at(parent, start)

    prices = variant.reconstitution.prices
    wide = prices.pivot_table(index="date", columns="security_id", values="close",
                              aggfunc="last").sort_index()
    window = wide.loc[(wide.index >= start) & (wide.index <= end)]
    if len(window) < 2:
        raise ValueError("attribution needs at least two dates")
    security_returns = (window.iloc[-1] / window.iloc[0] - 1.0).dropna()

    ids = sorted(set(child_weights.index) | set(parent_weights.index))
    ids = [i for i in ids if i in security_returns.index]
    child = child_weights.reindex(ids).fillna(0.0)
    bench = parent_weights.reindex(ids).fillna(0.0)
    returns = security_returns.reindex(ids)

    industry = pd.Series(
        {i: str(variant.reconstitution._meta.get(i, {}).get("icb_industry", "?"))
         for i in ids}
    )

    brinson = brinson_fachler(child, bench, returns, returns, industry)
    effects = index_vs_parent(child, bench, returns, groups=industry)

    gtr = levels.set_index("date")["gross_total_return"]
    parent_gtr = parent.history.levels.set_index("date")["gross_total_return"]
    window_dates = [d for d in gtr.index if start <= d <= end]
    active = (
        float(gtr.loc[window_dates[-1]] / gtr.loc[window_dates[0]]
              - parent_gtr.loc[window_dates[-1]] / parent_gtr.loc[window_dates[0]])
        if window_dates else 0.0
    )

    notes: list[str] = []
    if not brinson.reconciles:
        notes.append(
            "Brinson components do not sum to the active return. That is expected here "
            "and worth stating: Brinson assumes weights held constant through the "
            "period, and this window contains reviews."
        )
    return AttributionAnalysis(
        period_start=start, period_end=end, active_return=active,
        brinson=brinson, index_effects=effects, notes=notes,
    )


def attribution_by_period(
    variant: VariantResult, parent: VariantResult, max_periods: int = 12
) -> pd.DataFrame:
    """Run Brinson over each review period and link the results.

    The correct way to attribute a multi-year horizon: attribute each single period,
    where the constant-weight assumption approximately holds, then link. Widening a
    single Brinson window to cover several rebalances does not measure the same thing
    and does not reconcile.

    Linking is geometric on the total and arithmetic on the components, which is the
    usual compromise: the components then sum to the *arithmetic* total, and the small
    gap between that and the compounded total is the linking residual - reported rather
    than hidden.
    """
    reviews = variant.history.reviews
    if reviews.empty:
        return pd.DataFrame()

    boundaries = list(reviews["date"])[-(max_periods + 1):]
    rows: list[dict[str, Any]] = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        try:
            analysis = analyse_attribution(variant, parent, start=start, end=end)
        except ValueError:
            continue
        rows.append({
            "period_start": start, "period_end": end,
            "active_return": analysis.active_return,
            "allocation": analysis.brinson.total_allocation,
            "selection": analysis.brinson.total_selection,
            "interaction": analysis.brinson.total_interaction,
            "brinson_total": analysis.brinson.total_active,
            "reconciles": analysis.brinson.reconciles,
        })

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    compounded = float((1 + frame["active_return"]).prod() - 1)
    frame.attrs["compounded_active_return"] = compounded
    frame.attrs["summed_allocation"] = float(frame["allocation"].sum())
    frame.attrs["summed_selection"] = float(frame["selection"].sum())
    frame.attrs["summed_interaction"] = float(frame["interaction"].sum())
    frame.attrs["linking_residual"] = compounded - float(
        frame[["allocation", "selection", "interaction"]].sum().sum())
    return frame


def factor_based_attribution(
    analysis: RiskAnalysis, variant: VariantResult, parent: VariantResult,
    end: dt.date | None = None,
) -> Any:
    """Decompose active return onto the risk model's factors.

    Reconciled against Brinson in the one-pager. The two disagree by construction —
    Brinson attributes to sectors, the factor model to styles, and a stock that is both
    "technology" and "high growth" lands in different buckets. Being able to say *why*
    they differ is the point; presenting either alone as "the" attribution is not.
    """
    factor_returns = analysis.model.factor_returns.returns
    end = end or variant.history.levels["date"].iloc[-1]
    window = factor_returns[factor_returns.index > analysis.as_of]
    cumulative = (1 + window).prod() - 1 if len(window) else window.sum()

    prices = variant.reconstitution.prices
    wide = prices.pivot_table(index="date", columns="security_id", values="close",
                              aggfunc="last").sort_index()
    band = wide.loc[(wide.index > analysis.as_of) & (wide.index <= end)]
    asset_returns = (band.iloc[-1] / band.iloc[0] - 1.0) if len(band) > 1 else pd.Series(
        dtype=float)

    active_weights = analysis.portfolio_weights - analysis.benchmark_weights
    return factor_attribution(
        active_weights=active_weights, exposures=analysis.model.exposures,
        factor_returns=pd.Series(cumulative), asset_returns=asset_returns,
    )


# --------------------------------------------------------------------------------------
# Client documents
# --------------------------------------------------------------------------------------


def write_risk_onepager(analysis: RiskAnalysis, variant_name: str, parent_name: str,
                        out: Path) -> Path:
    """A one-page risk report a non-technical reader can follow."""
    summary = analysis.summary()
    decomposition = analysis.decomposition
    factors = decomposition[decomposition["type"] == "factor"].head(8)
    total = float(decomposition["variance_contribution"].sum()) or 1.0

    lines = [
        f"# Risk report — {variant_name}",
        "",
        f"**Against** {parent_name} · **As at** {analysis.as_of} · "
        f"**Model** fundamental factor model, {len(analysis.model.factors)} factors",
        "",
        "---",
        "",
        "## Headline",
        "",
        f"Forecast tracking error: **{analysis.ex_ante_te:.2%} a year.**",
        "",
        "Tracking error is how far this index is expected to move away from its parent "
        "over a year. Roughly two years in three the difference should fall within "
        f"plus or minus {analysis.ex_ante_te:.1%}; in one year in twenty it could be "
        f"more than twice that.",
        "",
        "## Where the risk comes from",
        "",
        "| Source | Exposure | Share of risk |",
        "|---|---:|---:|",
    ]
    for row in factors.itertuples(index=False):
        exposure = "-" if not np.isfinite(row.exposure) else f"{row.exposure:+.3f}"
        lines.append(
            f"| {_pretty(row.source)} | {exposure} | "
            f"{row.variance_contribution / total:.1%} |"
        )
    specific = decomposition[decomposition["type"] == "specific"]
    if not specific.empty:
        lines.append(
            f"| Stock-specific | - | "
            f"{float(specific['variance_contribution'].iloc[0]) / total:.1%} |"
        )
    lines += [
        "",
        f"Factor risk accounts for {summary['factor_share_of_risk']:.0%} of the total "
        f"and stock-specific risk for {summary['specific_share_of_risk']:.0%}. A high "
        "specific share means the index is taking risk the model cannot name — which is "
        "either genuine diversifiable risk or a factor the model is missing.",
        "",
        "## Largest individual contributors",
        "",
        "| Security | Active weight | Marginal contribution to risk |",
        "|---|---:|---:|",
    ]
    active = analysis.portfolio_weights - analysis.benchmark_weights
    for sec in analysis.marginal.abs().nlargest(5).index:
        lines.append(
            f"| {sec} | {float(active.get(sec, 0.0)):+.2%} | "
            f"{float(analysis.marginal[sec]):+.2%} |"
        )

    lines += [
        "",
        "Marginal contribution is how much the index's overall risk would rise if the "
        "position were increased slightly. A large marginal contribution is not "
        "automatically bad — it is only bad if nothing is expected in return for it.",
        "",
        "## Is the forecast any good?",
        "",
        f"- Forecast at {analysis.as_of}: **{analysis.ex_ante_te:.2%}**",
        f"- Realised over the following period: **{analysis.ex_post_te:.2%}**"
        if np.isfinite(analysis.ex_post_te) else "- Realised: insufficient data",
        f"- Verdict: {analysis.bias_verdict}",
        "",
        "The forecast was made using data available at the estimation date only, and "
        "compared with what happened afterwards. A model tested on the period it was "
        "fitted to will always look accurate and tells you nothing.",
        "",
        "---",
        "",
        "*Risk figures are forecasts, not guarantees. This index is calculated on "
        "simulated market data and is not an investable benchmark.*",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_attribution_onepager(
    analysis: AttributionAnalysis, variant_name: str, parent_name: str, out: Path
) -> Path:
    """Why the index differed from its parent, in language a trustee can follow."""
    brinson = analysis.brinson
    effects = analysis.index_effects
    direction = "ahead of" if analysis.active_return >= 0 else "behind"

    lines = [
        f"# Performance attribution — {variant_name}",
        "",
        f"**Against** {parent_name} · "
        f"**Period** {analysis.period_start} to {analysis.period_end}",
        "",
        "---",
        "",
        "## Headline",
        "",
        f"The index finished **{abs(analysis.active_return):.2%} {direction}** its "
        "parent over the period.",
        "",
        "## What caused it — by design decision",
        "",
        "This is the decomposition that maps onto choices we actually made.",
        "",
        "| Component | Contribution | Detail |",
        "|---|---:|---|",
    ]
    for row in effects.components.itertuples(index=False):
        lines.append(
            f"| {_pretty(row.component)} | {row.contribution:+.2%} | {row.note} |")
    lines.append(
        f"| Unexplained residual | {effects.unexplained:+.2%} | "
        "not attributable to an identified effect |")

    lines += [
        "",
        "## What caused it — by sector",
        "",
        "The traditional view. *Allocation* is the effect of holding more or less of a "
        "sector than the parent; *selection* is the effect of holding different "
        "companies within a sector.",
        "",
        "| Sector | Active weight | Allocation | Selection | Total |",
        "|---|---:|---:|---:|---:|",
    ]
    top = brinson.by_group.reindex(
        brinson.by_group["total"].abs().sort_values(ascending=False).index).head(6)
    for row in top.itertuples(index=False):
        lines.append(
            f"| {row.group} | {row.active_weight:+.2%} | {row.allocation:+.3%} | "
            f"{row.selection:+.3%} | {row.total:+.3%} |"
        )
    lines += [
        "",
        f"**{brinson.summary()}**",
        "",
    ]
    if analysis.notes:
        lines += ["> " + n for n in analysis.notes] + [""]

    lines += [
        "## Reading this",
        "",
        "The two tables above will not agree, and that is expected rather than an "
        "error. The first attributes the difference to the design decisions that "
        "created this index — which securities it holds and how it weights them. The "
        "second attributes it to sectors. A company that is both a technology company "
        "and an expensive one appears in different places in each.",
        "",
        "The first table is the more useful of the two, because every line in it "
        "corresponds to something that could be changed.",
        "",
        "---",
        "",
        "*Calculated on simulated market data. Not an investable benchmark, and not "
        "a forecast of future results.*",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def build_backtest_live_bridge(
    variant: VariantResult, live_start: dt.date, cost_bps: float = 15.0
) -> pd.DataFrame:
    """Reconcile a pro-forma backtest return to a simulated live period.

    Splits the history at `live_start` and quantifies each identified difference. What
    is left is reported as an unexplained residual rather than absorbed into the nearest
    line, because the size of the residual is the honest measure of how well the
    reconciliation is understood.
    """
    levels = variant.history.levels.set_index("date")["gross_total_return"]
    backtest = levels[levels.index <= live_start]
    live = levels[levels.index >= live_start]
    if len(backtest) < 2 or len(live) < 2:
        raise ValueError("need data on both sides of the live start date")

    def annualise(series: pd.Series) -> float:
        years = (series.index[-1] - series.index[0]).days / 365.25
        return float((series.iloc[-1] / series.iloc[0]) ** (1 / years) - 1) if years \
            else 0.0

    backtest_return = annualise(backtest)
    live_return = annualise(live)

    reviews = variant.history.reviews
    annual_turnover = (
        float(reviews[reviews["date"] >= live_start]["one_way_turnover"].mean())
        * len(variant.history.config.review.months) if not reviews.empty else 0.0
    )
    cost_drag = -annual_turnover * 2 * cost_bps / 10_000

    bridge = BacktestLiveBridge()
    bridge.add(
        "transaction costs", cost_drag,
        f"{annual_turnover:.1%} annual one-way turnover at {cost_bps:.0f}bp "
        "round-trip. The index itself does not trade, but a fund tracking it does.",
    )
    bridge.add(
        "data vintage", 0.0,
        "Both periods use the same generated dataset, so there is no restatement "
        "difference here. On real data this line is usually the largest.",
    )
    bridge.add(
        "pro-forma reviews", 0.0,
        "Reviews were applied on the published schedule in both periods. A real live "
        "period carries committee exceptions and fast entries that a backtest does not.",
    )
    return bridge.build(backtest_return, live_return)


def _pretty(name: str) -> str:
    return str(name).replace("ind_", "Industry ").replace("cty_", "Country ").replace(
        "_", " ").strip().capitalize()
