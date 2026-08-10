"""Factor definitions, precise enough to publish.

Every provider defines these differently and that variation is itself a product
decision, not sloppiness: FTSE's value is not MSCI's value, and a client choosing
between them is choosing between definitions. So each factor here states its
sub-signals, its weights, and the specific choices that would otherwise be invisible.

Point-in-time throughout. Every fundamental comes from a filing dated on or before the
computation date; the trailing-twelve-month sums collapse restatements to the latest
filing *known then*, not the latest filing that exists.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from miniftse.factors.pipeline import (
    FactorPipeline,
    MissingDataPolicy,
    PipelineSpec,
    combine_scores,
)


@dataclass(frozen=True, slots=True)
class SubSignal:
    """One measurable component of a factor."""

    name: str
    description: str
    compute: Callable[[FactorInputs], pd.Series]
    weight: float = 1.0
    higher_is_better: bool = True


@dataclass
class FactorInputs:
    """The cross-section a factor is computed from, all as at one date.

    Frames are indexed by security_id. Fundamentals are trailing-twelve-month or
    latest-balance-sheet values already resolved point-in-time by the caller.
    """

    as_of: dt.date
    price: pd.Series
    market_cap: pd.Series
    """Full market cap in base currency."""

    float_market_cap: pd.Series
    industry: pd.Series
    country: pd.Series
    fundamentals: pd.DataFrame
    """Columns are item names: BOOK_EQUITY, NET_INCOME, REVENUE, TOTAL_ASSETS,
    TOTAL_DEBT, GROSS_PROFIT, OPERATING_CASHFLOW, CAPEX, DIVIDENDS_PAID."""

    returns: pd.DataFrame | None = None
    """Daily returns, dates x securities, ending at `as_of`. Needed by momentum and
    low-volatility."""

    prior_fundamentals: pd.DataFrame | None = None
    """The same items one year earlier, for growth and asset-growth signals."""

    def item(self, name: str) -> pd.Series:
        if name not in self.fundamentals.columns:
            return pd.Series(np.nan, index=self.price.index)
        return self.fundamentals[name].reindex(self.price.index)

    def prior_item(self, name: str) -> pd.Series:
        if self.prior_fundamentals is None or name not in self.prior_fundamentals:
            return pd.Series(np.nan, index=self.price.index)
        return self.prior_fundamentals[name].reindex(self.price.index)


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Ratio with a guarded denominator.

    Negative denominators are set to NaN rather than passed through. A negative
    book-to-price is not "very cheap": book equity below zero means the ratio has
    changed meaning, and letting it through puts distressed companies at the top of the
    value factor - a classic way to build a factor that looks great until it is traded.
    """
    d = denominator.where(denominator > 0)
    return numerator / d


# --------------------------------------------------------------------------------------
# Value
# --------------------------------------------------------------------------------------


def book_to_price(x: FactorInputs) -> pd.Series:
    return _safe_divide(x.item("BOOK_EQUITY"), x.market_cap)


def earnings_to_price(x: FactorInputs) -> pd.Series:
    """Earnings yield. Negative earnings are kept.

    Deliberately: a loss-making company genuinely has a negative earnings yield, and
    censoring it would flatter the factor by hiding its worst holdings. This is the
    opposite call from negative book value, where the ratio stops being meaningful
    rather than merely being bad.
    """
    return x.item("NET_INCOME") / x.market_cap.where(x.market_cap > 0)


def cashflow_to_price(x: FactorInputs) -> pd.Series:
    fcf = x.item("OPERATING_CASHFLOW") + x.item("CAPEX")  # capex is signed negative
    return fcf / x.market_cap.where(x.market_cap > 0)


def sales_to_price(x: FactorInputs) -> pd.Series:
    return _safe_divide(x.item("REVENUE"), x.market_cap)


def dividend_yield(x: FactorInputs) -> pd.Series:
    return (-x.item("DIVIDENDS_PAID")).clip(lower=0) / x.market_cap.where(x.market_cap > 0)


# --------------------------------------------------------------------------------------
# Quality
# --------------------------------------------------------------------------------------


def gross_profitability(x: FactorInputs) -> pd.Series:
    """Gross profit over total assets (Novy-Marx).

    Uses gross profit rather than net income on purpose: it sits above the line for
    R&D, advertising and depreciation policy, so it is far harder to manage and far
    more comparable across accounting regimes.
    """
    return _safe_divide(x.item("GROSS_PROFIT"), x.item("TOTAL_ASSETS"))


def return_on_equity(x: FactorInputs) -> pd.Series:
    return _safe_divide(x.item("NET_INCOME"), x.item("BOOK_EQUITY"))


def accruals(x: FactorInputs) -> pd.Series:
    """Earnings not backed by cash, scaled by assets. Lower is better.

    High accruals signal aggressive revenue recognition and reliably precede
    disappointment. Sign is flipped at the sub-signal level via `higher_is_better`.
    """
    return _safe_divide(x.item("NET_INCOME") - x.item("OPERATING_CASHFLOW"),
                        x.item("TOTAL_ASSETS"))


def leverage(x: FactorInputs) -> pd.Series:
    """Debt over assets. Lower is better."""
    return _safe_divide(x.item("TOTAL_DEBT"), x.item("TOTAL_ASSETS"))


def earnings_stability(x: FactorInputs) -> pd.Series:
    """Inverse coefficient of variation of return on assets.

    Needs a history this cross-section does not carry, so it falls back to a
    profitability proxy. Flagged rather than silently omitted: a factor definition that
    quietly drops a stated sub-signal is exactly the ambiguity the methodology-review
    exercise is meant to catch.
    """
    return gross_profitability(x)


# --------------------------------------------------------------------------------------
# Momentum, volatility, growth
# --------------------------------------------------------------------------------------


def momentum_12_1(x: FactorInputs) -> pd.Series:
    """Twelve-month return skipping the most recent month.

    The skip is essential. The last month carries short-term reversal, which has the
    opposite sign, and including it materially weakens the signal. It also happens to
    make the factor cheaper to trade, because the most recent month is where the
    crowding is.
    """
    if x.returns is None or len(x.returns) < 252:
        return pd.Series(np.nan, index=x.price.index)
    window = x.returns.iloc[-252:-21]
    return (1.0 + window).prod() - 1.0


def momentum_6_1(x: FactorInputs) -> pd.Series:
    if x.returns is None or len(x.returns) < 147:
        return pd.Series(np.nan, index=x.price.index)
    return (1.0 + x.returns.iloc[-126:-21]).prod() - 1.0


def short_term_reversal(x: FactorInputs) -> pd.Series:
    """Last month's return, negated. Included as a control, not as an index factor -
    it is real but its turnover makes it uninvestable in an index wrapper."""
    if x.returns is None or len(x.returns) < 21:
        return pd.Series(np.nan, index=x.price.index)
    return -((1.0 + x.returns.iloc[-21:]).prod() - 1.0)


def realised_volatility(x: FactorInputs, window: int = 252) -> pd.Series:
    """Annualised daily volatility. Lower is better for the low-vol factor."""
    if x.returns is None or len(x.returns) < 60:
        return pd.Series(np.nan, index=x.price.index)
    return x.returns.iloc[-window:].std() * np.sqrt(252)


def downside_volatility(x: FactorInputs, window: int = 252) -> pd.Series:
    if x.returns is None or len(x.returns) < 60:
        return pd.Series(np.nan, index=x.price.index)
    r = x.returns.iloc[-window:]
    return r.where(r < 0).std() * np.sqrt(252)


def beta_to_market(x: FactorInputs, window: int = 252) -> pd.Series:
    if x.returns is None or len(x.returns) < 60:
        return pd.Series(np.nan, index=x.price.index)
    r = x.returns.iloc[-window:]
    market = r.mean(axis=1)
    var = market.var()
    if var <= 0:
        return pd.Series(np.nan, index=x.price.index)
    return r.apply(lambda col: col.cov(market) / var)


def log_market_cap(x: FactorInputs) -> pd.Series:
    """Size. Negated at the sub-signal level so a high score means small."""
    return np.log(x.float_market_cap.clip(lower=1.0))


def asset_growth(x: FactorInputs) -> pd.Series:
    """Year-on-year asset growth. Lower is better - the investment factor.

    Companies that grow the balance sheet fast subsequently underperform, whether the
    story is empire-building or just mean reversion in returns on capital.
    """
    prior = x.prior_item("TOTAL_ASSETS")
    return _safe_divide(x.item("TOTAL_ASSETS") - prior, prior)


def sales_growth(x: FactorInputs) -> pd.Series:
    prior = x.prior_item("REVENUE")
    return _safe_divide(x.item("REVENUE") - prior, prior)


# --------------------------------------------------------------------------------------
# Factor definitions
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FactorDefinition:
    """A complete, publishable factor specification."""

    name: str
    rationale: str
    """The economic story. A factor without one is a data-mining result, and at an
    index provider that distinction is a governance matter, not a philosophical one."""

    sub_signals: tuple[SubSignal, ...]
    spec: PipelineSpec = field(default_factory=PipelineSpec)
    combination: str = "integrated"

    def compute(self, x: FactorInputs) -> pd.Series:
        pipeline = FactorPipeline(self.spec)
        scores: dict[str, pd.Series] = {}
        weights: dict[str, float] = {}
        for sub in self.sub_signals:
            raw = sub.compute(x)
            if not sub.higher_is_better:
                raw = -raw
            scores[sub.name] = pipeline.transform(
                raw, industry=x.industry, country=x.country, market_cap=x.market_cap
            )
            weights[sub.name] = sub.weight
        combined = combine_scores(scores, weights, self.combination)
        # Re-standardise the composite: averaging correlated sub-scores shrinks the
        # dispersion, so without this the tilt strength would mean something different
        # for a one-signal factor than for a four-signal one.
        return pipeline.transform(
            combined, industry=x.industry, country=x.country, market_cap=x.market_cap
        )

    def sub_scores(self, x: FactorInputs) -> pd.DataFrame:
        """Each sub-signal separately, for the diagnostics a methodology review needs."""
        pipeline = FactorPipeline(self.spec)
        out = {}
        for sub in self.sub_signals:
            raw = sub.compute(x)
            if not sub.higher_is_better:
                raw = -raw
            out[sub.name] = pipeline.transform(raw, x.industry, x.country, x.market_cap)
        return pd.DataFrame(out)

    def describe(self) -> str:
        """Methodology prose, generated from the definition."""
        lines = [
            f"### {self.name}",
            "",
            self.rationale,
            "",
            "**Sub-signals**",
            "",
            "| Signal | Definition | Weight | Direction |",
            "|---|---|---|---|",
        ]
        total = sum(s.weight for s in self.sub_signals)
        lines += [
            f"| {s.name} | {s.description} | {s.weight / total:.0%} | "
            f"{'higher is better' if s.higher_is_better else 'lower is better'} |"
            for s in self.sub_signals
        ]
        lines += ["", "**Processing**", "", self.spec.describe(), ""]
        lines.append(
            f"Sub-signals are combined using the *{self.combination}* method and the "
            "composite is re-standardised."
        )
        return "\n".join(lines)


VALUE = FactorDefinition(
    name="Value",
    rationale=(
        "Securities that are cheap relative to fundamentals have historically earned a "
        "premium. The economic story is contested and the choice between the two "
        "readings matters for how the index is sold: either cheap companies are riskier "
        "in ways a single-factor model misses and the premium is compensation, or "
        "investors systematically over-extrapolate bad news and the premium is a "
        "behavioural correction. The first implies the premium persists; the second "
        "implies it decays once it is widely known."
    ),
    sub_signals=(
        SubSignal("book_to_price", "Book equity / full market cap", book_to_price, 0.30),
        SubSignal("earnings_to_price", "Trailing 12m net income / market cap",
                  earnings_to_price, 0.25),
        SubSignal("cashflow_to_price", "Operating cash flow less capex / market cap",
                  cashflow_to_price, 0.25),
        SubSignal("sales_to_price", "Trailing 12m revenue / market cap",
                  sales_to_price, 0.20),
    ),
)

QUALITY = FactorDefinition(
    name="Quality",
    rationale=(
        "Profitable, conservatively financed companies with earnings backed by cash "
        "outperform on a risk-adjusted basis. Gross profitability carries most of the "
        "weight because it is the hardest line for management to influence and the most "
        "comparable across accounting regimes."
    ),
    sub_signals=(
        SubSignal("gross_profitability", "Gross profit / total assets",
                  gross_profitability, 0.35),
        SubSignal("return_on_equity", "Trailing 12m net income / book equity",
                  return_on_equity, 0.25),
        SubSignal("accruals", "(Net income - operating cash flow) / total assets",
                  accruals, 0.20, higher_is_better=False),
        SubSignal("leverage", "Total debt / total assets", leverage, 0.20,
                  higher_is_better=False),
    ),
)

MOMENTUM = FactorDefinition(
    name="Momentum",
    rationale=(
        "Recent relative winners continue to outperform over horizons of three to twelve "
        "months, most plausibly because information diffuses slowly and investors "
        "under-react. Momentum is the highest-turnover of the classic factors and the "
        "one most damaged by transaction costs, which is exactly why an index wrapper "
        "needs a turnover budget rather than the raw signal."
    ),
    sub_signals=(
        SubSignal("momentum_12_1", "12-month return, skipping the most recent month",
                  momentum_12_1, 0.70),
        SubSignal("momentum_6_1", "6-month return, skipping the most recent month",
                  momentum_6_1, 0.30),
    ),
    spec=PipelineSpec(neutralise_industry=True, use_mad=True),
)

LOW_VOLATILITY = FactorDefinition(
    name="Low Volatility",
    rationale=(
        "Low-risk securities have delivered higher risk-adjusted returns than the CAPM "
        "predicts. The leading explanation is leverage-constrained investors bidding up "
        "high-beta names to reach return targets they cannot reach with borrowing. Note "
        "the consequence for index design: the factor is defined on risk, so it is "
        "mechanically correlated with sector composition and demands industry "
        "neutralisation or it becomes a utilities-and-staples bet."
    ),
    sub_signals=(
        SubSignal("realised_volatility", "Annualised 12-month daily volatility",
                  realised_volatility, 0.50, higher_is_better=False),
        SubSignal("downside_volatility", "Annualised volatility of negative days",
                  downside_volatility, 0.25, higher_is_better=False),
        SubSignal("beta", "12-month beta to the equal-weighted universe",
                  beta_to_market, 0.25, higher_is_better=False),
    ),
)

SIZE = FactorDefinition(
    name="Size",
    rationale=(
        "Smaller companies have earned a premium over larger ones, though the effect is "
        "weak once microcaps and illiquid names are excluded, and much of the original "
        "evidence does not survive a realistic liquidity screen. Included because it is "
        "a standard risk-model factor and a necessary control, not because it is a "
        "compelling standalone index."
    ),
    sub_signals=(
        SubSignal("log_market_cap", "Natural log of free-float market cap",
                  log_market_cap, 1.0, higher_is_better=False),
    ),
    spec=PipelineSpec(neutralise_industry=False, neutralise_size=False),
)

INVESTMENT = FactorDefinition(
    name="Investment",
    rationale=(
        "Companies that grow assets aggressively subsequently underperform. Consistent "
        "with both an over-investment story and simple mean reversion in returns on "
        "capital."
    ),
    sub_signals=(
        SubSignal("asset_growth", "Year-on-year growth in total assets", asset_growth,
                  0.60, higher_is_better=False),
        SubSignal("sales_growth", "Year-on-year growth in revenue", sales_growth, 0.40,
                  higher_is_better=False),
    ),
    spec=PipelineSpec(missing_policy=MissingDataPolicy.NEUTRAL),
)

YIELD = FactorDefinition(
    name="Yield",
    rationale=(
        "High dividend payers have delivered a modest premium and materially lower "
        "volatility. The index-design trap is a yield trap: the highest yielders are "
        "often companies whose price has collapsed on a dividend the market expects to "
        "be cut, so a yield index without a quality overlay systematically buys "
        "impending dividend cuts."
    ),
    sub_signals=(
        SubSignal("dividend_yield", "Trailing 12m dividends paid / market cap",
                  dividend_yield, 0.70),
        SubSignal("gross_profitability", "Gross profit / total assets, as a quality "
                  "overlay against yield traps", gross_profitability, 0.30),
    ),
)

ALL_FACTORS: dict[str, FactorDefinition] = {
    f.name.lower().replace(" ", "_"): f
    for f in (VALUE, QUALITY, MOMENTUM, LOW_VOLATILITY, SIZE, INVESTMENT, YIELD)
}


def compute_all(x: FactorInputs, factors: list[str] | None = None) -> pd.DataFrame:
    """Every factor score for one cross-section."""
    names = factors or list(ALL_FACTORS)
    return pd.DataFrame({n: ALL_FACTORS[n].compute(x) for n in names})


def factor_correlations(scores: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional correlation of factor scores.

    Read before combining. Value and quality are usually mildly negative - cheap
    companies tend to be worse companies - and value and momentum strongly so.
    A multi-factor index that ignores this ends up with far less net exposure than the
    sum of its parts suggests.
    """
    return scores.corr(method="spearman")
