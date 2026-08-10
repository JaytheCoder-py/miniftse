"""Information coefficients, signal decay, and research integrity.

Two halves. The first measures how well a signal predicts: rank IC, its decay by
horizon, and what that implies for rebalance frequency. The second is the part that
makes the first trustworthy - multiple-testing corrections, the deflated Sharpe ratio,
and a degradation waterfall from paper result to investable result.

The second half matters more here than in most settings. A hedge fund that fools itself
loses its own money. An index provider that fools itself publishes rules that other
people put billions behind, and the error surfaces years later as underperformance
against a backtest nobody can reproduce.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats


@dataclass
class ICResult:
    """Information coefficient statistics for one signal at one horizon."""

    horizon: int
    ic_series: pd.Series
    mean_ic: float
    std_ic: float
    ic_ir: float
    """Mean over standard deviation. The signal's own information ratio, and a better
    summary than the mean IC alone - a signal with IC 0.03 that is always positive is
    far more valuable than one averaging 0.05 with the sign flipping."""

    t_stat: float
    p_value: float
    hit_rate: float
    n_periods: int
    method: str = "spearman"

    def implied_breadth(self, target_ir: float = 0.5) -> float:
        """Breadth needed to reach a target information ratio.

        From the fundamental law, ``IR ≈ IC × sqrt(breadth)``. Treat the output as an
        order of magnitude, not a number: the law assumes independent bets, and cross-
        sectional equity signals are anything but - a value signal makes one bet
        expressed 500 ways, not 500 bets.
        """
        if self.mean_ic == 0:
            return float("inf")
        return (target_ir / self.mean_ic) ** 2

    def summary(self) -> dict[str, float]:
        return {
            "horizon_days": self.horizon, "mean_ic": self.mean_ic,
            "std_ic": self.std_ic, "ic_ir": self.ic_ir, "t_stat": self.t_stat,
            "p_value": self.p_value, "hit_rate": self.hit_rate,
            "n_periods": self.n_periods,
        }


def information_coefficient(
    scores: pd.DataFrame,
    forward_returns: pd.DataFrame,
    method: str = "spearman",
    min_obs: int = 20,
) -> pd.Series:
    """Period-by-period cross-sectional correlation of score with forward return.

    Spearman by default. Pearson is dominated by the extremes of both distributions,
    and a single stock that fell 80% can flip the sign of a month's IC - which is a
    statement about that stock, not about the signal.
    """
    common_dates = scores.index.intersection(forward_returns.index)
    out: dict[object, float] = {}
    for date in common_dates:
        s = scores.loc[date]
        r = forward_returns.loc[date]
        frame = pd.concat([s.rename("s"), r.rename("r")], axis=1).dropna()
        if len(frame) < min_obs:
            continue
        if frame["s"].nunique() < 3 or frame["r"].nunique() < 3:
            continue
        corr = (
            stats.spearmanr(frame["s"], frame["r"]).correlation
            if method == "spearman"
            else float(np.corrcoef(frame["s"], frame["r"])[0, 1])
        )
        if np.isfinite(corr):
            out[date] = float(corr)
    return pd.Series(out).sort_index()


def analyse_ic(
    scores: pd.DataFrame,
    forward_returns: pd.DataFrame,
    horizon: int = 21,
    method: str = "spearman",
) -> ICResult:
    ic = information_coefficient(scores, forward_returns, method)
    if ic.empty:
        raise ValueError("no periods produced an information coefficient")

    mean, std = float(ic.mean()), float(ic.std(ddof=1))
    n = len(ic)
    # Newey-West on the IC series: overlapping forward windows make consecutive ICs
    # mechanically correlated, so the naive t-statistic overstates significance by
    # roughly sqrt(horizon/period).
    from miniftse.research.regression import newey_west_se

    se, _ = newey_west_se(ic.to_numpy())
    t = mean / se if se and np.isfinite(se) and se > 0 else float("nan")

    return ICResult(
        horizon=horizon, ic_series=ic, mean_ic=mean, std_ic=std,
        ic_ir=mean / std if std > 0 else 0.0, t_stat=float(t),
        p_value=float(2 * (1 - stats.t.cdf(abs(t), df=n - 1))) if np.isfinite(t) else 1.0,
        hit_rate=float((ic > 0).mean()), n_periods=n, method=method,
    )


def ic_decay(
    scores: pd.DataFrame,
    returns: pd.DataFrame,
    horizons: tuple[int, ...] = (1, 5, 21, 63, 126, 252),
) -> pd.DataFrame:
    """IC at increasing horizons.

    The shape is the actionable output. A signal whose IC peaks at one day and is gone
    by a month cannot be put in a quarterly-rebalanced index at any turnover budget. One
    that holds its IC out to six months can be traded slowly, which is what makes it
    viable in an index wrapper at all.
    """
    rows = []
    for h in horizons:
        fwd = forward_return_panel(returns, h)
        common = scores.index.intersection(fwd.index)
        if len(common) < 12:
            continue
        try:
            result = analyse_ic(scores.loc[common], fwd.loc[common], horizon=h)
        except ValueError:
            continue
        rows.append(result.summary())
    frame = pd.DataFrame(rows)
    if not frame.empty:
        # IC per unit of holding period: which horizon gives most information per unit
        # of turnover, which is the rebalance-frequency question.
        frame["ic_per_sqrt_horizon"] = frame["mean_ic"] / np.sqrt(frame["horizon_days"])
    return frame


def forward_return_panel(returns: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Forward compounded return over `horizon` sessions, aligned to the decision date.

    Row ``t`` holds the return from ``t`` to ``t+horizon``. Aligning to the *end* of the
    window instead is the single most effective way to produce a signal with an IC of
    0.4 that makes no money.
    """
    fwd = (1.0 + returns).rolling(horizon).apply(np.prod, raw=True) - 1.0
    return fwd.shift(-horizon)


def quantile_returns(
    scores: pd.DataFrame,
    forward_returns: pd.DataFrame,
    n_quantiles: int = 5,
) -> pd.DataFrame:
    """Mean forward return by score quantile, period by period.

    The monotonicity of the quantile spread matters as much as the top-minus-bottom
    number. A signal that works only in the extreme decile is a short-selling strategy,
    not something a long-only index can express.
    """
    rows = []
    for date in scores.index.intersection(forward_returns.index):
        frame = pd.concat(
            [scores.loc[date].rename("s"), forward_returns.loc[date].rename("r")], axis=1
        ).dropna()
        if len(frame) < n_quantiles * 3:
            continue
        frame["q"] = pd.qcut(frame["s"].rank(method="first"), n_quantiles,
                             labels=range(1, n_quantiles + 1))
        means = frame.groupby("q", observed=True)["r"].mean()
        rows.append({"date": date, **{f"Q{int(q)}": v for q, v in means.items()}})

    out = pd.DataFrame(rows).set_index("date")
    if not out.empty and f"Q{n_quantiles}" in out and "Q1" in out:
        out["long_short"] = out[f"Q{n_quantiles}"] - out["Q1"]
        out["monotonic"] = out[[f"Q{i}" for i in range(1, n_quantiles + 1)]].apply(
            lambda r: bool(r.is_monotonic_increasing), axis=1
        )
    return out


# --------------------------------------------------------------------------------------
# Research integrity
# --------------------------------------------------------------------------------------


@dataclass
class MultipleTestingResult:
    n_tests: int
    raw_significant: int
    bonferroni_significant: int
    bh_significant: int
    bonferroni_threshold: float
    implied_t_threshold: float
    survivors: list[str] = field(default_factory=list)

    def verdict(self) -> str:
        return (
            f"{self.n_tests} signals tested. {self.raw_significant} clear the "
            f"conventional |t| > 2 bar. After Bonferroni "
            f"{self.bonferroni_significant} survive; under Benjamini-Hochberg at 10% "
            f"false-discovery rate, {self.bh_significant} do. A new factor should be "
            f"required to clear |t| > {self.implied_t_threshold:.2f}."
        )


def multiple_testing(
    t_stats: dict[str, float],
    n_effective_tests: int | None = None,
    alpha: float = 0.05,
    fdr: float = 0.10,
) -> MultipleTestingResult:
    """Bonferroni and Benjamini-Hochberg corrections.

    `n_effective_tests` should reflect every specification that was *tried*, not the
    number reported. Harvey, Liu and Zhu argue the true count across the published
    factor literature runs into the many hundreds once unpublished attempts are
    accounted for, which pushes the honest critical value for a genuinely new factor to
    roughly |t| > 3.
    """
    names = list(t_stats)
    t = np.array([t_stats[n] for n in names], dtype=float)
    p = 2 * (1 - stats.norm.cdf(np.abs(t)))
    m = n_effective_tests or len(names)

    bonf_threshold = alpha / m
    bonf = p < bonf_threshold

    order = np.argsort(p)
    ranks = np.arange(1, len(p) + 1)
    bh_line = fdr * ranks / len(p)
    passing = p[order] <= bh_line
    cutoff = int(np.max(np.flatnonzero(passing)) + 1) if passing.any() else 0
    bh = np.zeros(len(p), dtype=bool)
    bh[order[:cutoff]] = True

    return MultipleTestingResult(
        n_tests=len(names),
        raw_significant=int((np.abs(t) > 2.0).sum()),
        bonferroni_significant=int(bonf.sum()),
        bh_significant=int(bh.sum()),
        bonferroni_threshold=float(bonf_threshold),
        implied_t_threshold=float(stats.norm.ppf(1 - bonf_threshold / 2)),
        survivors=[names[i] for i in np.flatnonzero(bh)],
    )


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_trials: int,
    n_observations: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    benchmark_sharpe: float = 0.0,
) -> dict[str, float]:
    """Bailey and Lopez de Prado's deflated Sharpe ratio.

    Answers: given that this was the best of `n_trials` attempts, what is the
    probability the true Sharpe exceeds the benchmark? The expected maximum Sharpe from
    pure noise across many trials is surprisingly high, which is why the best backtest
    out of fifty tells you almost nothing on its own.
    """
    if n_trials < 1 or n_observations < 2:
        raise ValueError("need at least one trial and two observations")

    euler = 0.5772156649
    e_max = (
        (1 - euler) * stats.norm.ppf(1 - 1.0 / n_trials)
        + euler * stats.norm.ppf(1 - 1.0 / (n_trials * np.e))
    ) if n_trials > 1 else 0.0

    numerator = (observed_sharpe - max(benchmark_sharpe, e_max)) * np.sqrt(
        n_observations - 1
    )
    denominator = np.sqrt(
        1 - skewness * observed_sharpe + (kurtosis - 1) / 4 * observed_sharpe**2
    )
    dsr = float(stats.norm.cdf(numerator / denominator)) if denominator > 0 else 0.0

    return {
        "observed_sharpe": observed_sharpe,
        "expected_max_from_noise": float(e_max),
        "deflated_sharpe_probability": dsr,
        "n_trials": float(n_trials),
        "passes": float(dsr > 0.95),
    }


@dataclass
class DegradationStep:
    name: str
    sharpe: float
    description: str

    @property
    def label(self) -> str:
        return f"{self.name}: {self.sharpe:.2f}"


def degradation_waterfall(steps: list[DegradationStep]) -> pd.DataFrame:
    """From paper Sharpe to investable Sharpe, one honest deduction at a time.

    The canonical sequence: remove microcaps, apply a liquidity screen, add transaction
    costs, impose an implementation lag, then look only at data published after the
    original paper. Each is a real-world constraint that the paper version did not face.

    Most published anomalies lose the majority of their Sharpe to this sequence, and
    a good number lose all of it. Producing this chart for your own signal before
    anyone asks is the single most credible thing a researcher can bring to a review.
    """
    if not steps:
        return pd.DataFrame()
    rows = []
    prior = steps[0].sharpe
    for i, step in enumerate(steps):
        delta = step.sharpe - prior if i else 0.0
        rows.append({
            "step": step.name,
            "sharpe": step.sharpe,
            "change": delta,
            "cumulative_loss": steps[0].sharpe - step.sharpe,
            "pct_of_original": step.sharpe / steps[0].sharpe if steps[0].sharpe else 0.0,
            "description": step.description,
        })
        prior = step.sharpe
    return pd.DataFrame(rows)


def random_signal_benchmark(
    returns: pd.DataFrame, n_signals: int = 200, seed: int = 42
) -> dict[str, float]:
    """Distribution of t-statistics from signals with zero true predictive power.

    Generate `n_signals` random cross-sectional signals on real returns and record what
    the best one achieves. It is routinely above 2.5 and often above 3, which is the
    demonstration that makes the multiple-testing argument concrete: that number is
    what a factor has to beat before it has said anything at all.
    """
    rng = np.random.default_rng(seed)
    fwd = returns.shift(-21).rolling(21).sum().shift(-20)
    t_stats: list[float] = []

    for _ in range(n_signals):
        signal = pd.DataFrame(
            rng.standard_normal(returns.shape), index=returns.index,
            columns=returns.columns,
        )
        ic = information_coefficient(signal.iloc[::21], fwd.iloc[::21])
        if len(ic) < 12:
            continue
        se = ic.std(ddof=1) / np.sqrt(len(ic))
        if se > 0:
            t_stats.append(float(ic.mean() / se))

    arr = np.array(t_stats)
    return {
        "n_signals": float(len(arr)),
        "max_abs_t": float(np.abs(arr).max()) if arr.size else float("nan"),
        "pct_above_2": float((np.abs(arr) > 2).mean()) if arr.size else float("nan"),
        "pct_above_3": float((np.abs(arr) > 3).mean()) if arr.size else float("nan"),
        "p95_abs_t": float(np.percentile(np.abs(arr), 95)) if arr.size else float("nan"),
    }
