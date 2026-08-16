"""Cross-sectional regression: Fama-MacBeth, robust standard errors, the GRS test.

Written from first principles rather than wrapping `linearmodels`, because the point is
to know what the machinery does. The test suite reconciles the output against
`linearmodels.FamaMacBeth` where it is installed.

The one idea worth internalising: in a panel of stock returns, the cross-sectional
correlation is enormous - everything moves with the market - and pooled OLS treats every
stock-month as an independent observation. It therefore reports standard errors that can
be several times too small, which is how a factor with no real predictive power arrives
at a t-statistic of 3. Fama-MacBeth sidesteps this by estimating one coefficient per
period and testing the *time series* of coefficients, where the cross-sectional
correlation has already been absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats


@dataclass(frozen=True, slots=True)
class RegressionResult:
    """One cross-sectional regression."""

    date: object
    params: pd.Series
    n_obs: int
    r_squared: float
    residuals: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


@dataclass
class FamaMacBethResult:
    """The full Fama-MacBeth output."""

    coefficients: pd.DataFrame
    """Period-by-period coefficients. The primary artefact - the mean is the headline
    but the time series is where the story is."""

    means: pd.Series
    std_errors: pd.Series
    t_stats: pd.Series
    p_values: pd.Series
    n_periods: int
    mean_n_obs: float
    mean_r_squared: float
    newey_west_lags: int
    se_method: str

    def summary(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "coefficient": self.means,
                "std_error": self.std_errors,
                "t_stat": self.t_stats,
                "p_value": self.p_values,
                "annualised": self.means * 12,
            }
        ).round(6)

    def significant(self, threshold: float = 3.0) -> list[str]:
        """Names clearing a t-statistic threshold.

        The default is 3.0, not 2.0. Harvey, Liu and Zhu's point is that hundreds of
        factors have been tested against the same data, so the conventional 5% critical
        value is far too generous once you account for the search. Three is the widely
        cited floor for a *new* factor; see `multiple_testing`.
        """
        return [k for k, v in self.t_stats.items() if abs(v) >= threshold]


def newey_west_se(series: np.ndarray, lags: int | None = None) -> tuple[float, int]:
    """Heteroskedasticity- and autocorrelation-consistent standard error of a mean.

    Fama-MacBeth's own standard error assumes the period coefficients are independent
    over time. They usually are not - factor returns are persistent - so the naive
    standard error is too small and every t-statistic is inflated.

    Lag selection defaults to Newey and West's automatic rule, ``4*(T/100)^(2/9)``, which
    grows slowly with the sample. Choosing lags by hand until the result becomes
    significant is a real and common form of specification search.
    """
    x = np.asarray(series, dtype=float)
    x = x[np.isfinite(x)]
    t = x.size
    if t < 2:
        return float("nan"), 0
    if lags is None:
        lags = int(np.floor(4 * (t / 100.0) ** (2.0 / 9.0)))
        lags = max(0, min(lags, t - 1))

    demeaned = x - x.mean()
    gamma0 = float(demeaned @ demeaned) / t
    variance = gamma0
    for lag in range(1, lags + 1):
        gamma = float(demeaned[lag:] @ demeaned[:-lag]) / t
        bartlett = 1.0 - lag / (lags + 1.0)
        variance += 2.0 * bartlett * gamma
    variance = max(variance, 0.0)
    return float(np.sqrt(variance / t)), lags


def white_se(x: np.ndarray, residuals: np.ndarray) -> np.ndarray:
    """Heteroskedasticity-consistent (HC0) standard errors for one regression."""
    xtx_inv = np.linalg.pinv(x.T @ x)
    meat = x.T @ np.diag(residuals**2) @ x
    return np.sqrt(np.diag(xtx_inv @ meat @ xtx_inv))


def cluster_se(x: np.ndarray, residuals: np.ndarray, clusters: np.ndarray) -> np.ndarray:
    """Cluster-robust standard errors.

    Allows arbitrary correlation within a cluster. Clustering by date handles the
    cross-sectional correlation that sinks pooled OLS; clustering by firm handles
    persistence in a firm's residuals. Two-way clustering does both and is the honest
    choice for a stock-month panel.
    """
    xtx_inv = np.linalg.pinv(x.T @ x)
    meat = np.zeros((x.shape[1], x.shape[1]))
    for c in np.unique(clusters):
        mask = clusters == c
        xu = x[mask].T @ residuals[mask]
        meat += np.outer(xu, xu)
    n, k = x.shape
    g = len(np.unique(clusters))
    correction = (g / (g - 1)) * ((n - 1) / (n - k)) if g > 1 else 1.0
    return np.sqrt(np.diag(xtx_inv @ meat @ xtx_inv) * correction)


def cross_sectional_regression(
    y: pd.Series,
    x: pd.DataFrame,
    weights: pd.Series | None = None,
    add_constant: bool = True,
) -> RegressionResult | None:
    """One period's regression of forward returns on characteristics."""
    frame = pd.concat([y.rename("_y"), x], axis=1).dropna()
    if len(frame) < x.shape[1] + 2:
        return None

    yv = frame["_y"].to_numpy(dtype=float)
    xv = frame.drop(columns="_y")
    names = list(xv.columns)
    xm = xv.to_numpy(dtype=float)
    if add_constant:
        xm = np.column_stack([np.ones(len(xm)), xm])
        names = ["const", *names]

    if weights is not None:
        w = np.sqrt(weights.reindex(frame.index).fillna(0.0).to_numpy(dtype=float))
        w = np.where(w > 0, w, 1e-12)
        beta, *_ = np.linalg.lstsq(xm * w[:, None], yv * w, rcond=None)
    else:
        beta, *_ = np.linalg.lstsq(xm, yv, rcond=None)

    fitted = xm @ beta
    resid = yv - fitted
    ss_tot = float(((yv - yv.mean()) ** 2).sum())
    r2 = 1.0 - float((resid**2).sum()) / ss_tot if ss_tot > 0 else 0.0

    return RegressionResult(
        date=None,
        params=pd.Series(beta, index=names),
        n_obs=len(frame),
        r_squared=r2,
        residuals=pd.Series(resid, index=frame.index),
    )


def fama_macbeth(
    returns: pd.DataFrame,
    characteristics: dict[str, pd.DataFrame],
    weights: pd.DataFrame | None = None,
    newey_west_lags: int | None = None,
    min_obs: int = 20,
) -> FamaMacBethResult:
    """Fama-MacBeth over a panel.

    Parameters
    ----------
    returns
        Forward returns, dates x securities. Row ``t`` must hold the return earned
        *after* the characteristics at ``t`` were observable - the caller is responsible
        for the shift, and getting it backwards produces a spectacular and entirely
        fictitious result.
    characteristics
        One frame per regressor, each dates x securities.
    weights
        Optional regression weights. Weighting by square-root market cap is the usual
        compromise: unweighted lets microcaps dominate the fit, cap-weighting lets the
        top decile dominate.
    """
    dates = returns.index
    for frame in characteristics.values():
        dates = dates.intersection(frame.index)
    dates = dates.sort_values()

    rows: list[dict[str, float]] = []
    r2s: list[float] = []
    n_obs: list[int] = []

    for date in dates:
        y = returns.loc[date]
        x = pd.DataFrame({name: f.loc[date] for name, f in characteristics.items()})
        w = weights.loc[date] if weights is not None else None
        result = cross_sectional_regression(y, x, w)
        if result is None or result.n_obs < min_obs:
            continue
        rows.append({"date": date, **result.params.to_dict()})
        r2s.append(result.r_squared)
        n_obs.append(result.n_obs)

    if not rows:
        raise ValueError("no period had enough observations to estimate")

    coefficients = pd.DataFrame(rows).set_index("date")
    means = coefficients.mean()

    ses: dict[str, float] = {}
    lags_used = 0
    for col in coefficients.columns:
        se, lags = newey_west_se(coefficients[col].to_numpy(), newey_west_lags)
        ses[col] = se
        lags_used = lags

    se_series = pd.Series(ses)
    t_stats = means / se_series
    p_values = pd.Series(
        2 * (1 - stats.t.cdf(np.abs(t_stats), df=len(coefficients) - 1)),
        index=t_stats.index,
    )

    return FamaMacBethResult(
        coefficients=coefficients,
        means=means,
        std_errors=se_series,
        t_stats=t_stats,
        p_values=p_values,
        n_periods=len(coefficients),
        mean_n_obs=float(np.mean(n_obs)),
        mean_r_squared=float(np.mean(r2s)),
        newey_west_lags=lags_used,
        se_method="Newey-West",
    )


# --------------------------------------------------------------------------------------
# Time-series factor regressions
# --------------------------------------------------------------------------------------


@dataclass
class TimeSeriesRegression:
    alpha: float
    alpha_t: float
    betas: pd.Series
    beta_t: pd.Series
    r_squared: float
    n_obs: int
    residual_vol: float

    def summary(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "coefficient": pd.concat([pd.Series({"alpha": self.alpha}), self.betas]),
                "t_stat": pd.concat([pd.Series({"alpha": self.alpha_t}), self.beta_t]),
            }
        ).round(4)


def time_series_regression(
    portfolio_returns: pd.Series,
    factor_returns: pd.DataFrame,
    newey_west_lags: int | None = None,
) -> TimeSeriesRegression:
    """Regress a portfolio on factor returns to get alpha and loadings.

    CAPM, Fama-French 3, Carhart 4 and FF5 are all this function with different
    columns. The question it answers for an index provider is the one clients actually
    ask: is this index's return explained by known factors, or is there something left
    over?
    """
    frame = pd.concat([portfolio_returns.rename("_y"), factor_returns], axis=1).dropna()
    y = frame["_y"].to_numpy(dtype=float)
    x = np.column_stack([np.ones(len(frame)), frame.drop(columns="_y").to_numpy(float)])
    names = ["alpha", *factor_returns.columns]

    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ beta
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid**2).sum()) / ss_tot if ss_tot > 0 else 0.0

    # HAC standard errors on the coefficients, via the sandwich with a Bartlett kernel.
    t = len(y)
    lags = (
        newey_west_lags
        if newey_west_lags is not None
        else int(np.floor(4 * (t / 100.0) ** (2.0 / 9.0)))
    )
    xtx_inv = np.linalg.pinv(x.T @ x)
    u = x * resid[:, None]
    meat = u.T @ u
    for lag in range(1, max(lags, 0) + 1):
        gamma = u[lag:].T @ u[:-lag]
        w = 1.0 - lag / (lags + 1.0)
        meat += w * (gamma + gamma.T)
    cov = xtx_inv @ meat @ xtx_inv
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    tstats = beta / np.where(se > 0, se, np.nan)

    return TimeSeriesRegression(
        alpha=float(beta[0]),
        alpha_t=float(tstats[0]),
        betas=pd.Series(beta[1:], index=names[1:]),
        beta_t=pd.Series(tstats[1:], index=names[1:]),
        r_squared=r2,
        n_obs=t,
        residual_vol=float(resid.std() * np.sqrt(252)),
    )


def grs_test(portfolio_returns: pd.DataFrame, factor_returns: pd.DataFrame) -> dict[str, float]:
    """Gibbons-Ross-Shanken: are the alphas of N portfolios jointly zero?

    The right test when asking whether a factor model prices a set of portfolios.
    Testing each alpha separately and counting how many clear 2.0 is a multiple-testing
    error; GRS asks the joint question once.
    """
    common = portfolio_returns.index.intersection(factor_returns.index)
    r = portfolio_returns.loc[common].dropna(axis=1, how="any")
    f = factor_returns.loc[common]
    t, n = r.shape
    k = f.shape[1]
    if t <= n + k:
        return {"error": float("nan"), "note": float(t)}

    x = np.column_stack([np.ones(t), f.to_numpy(float)])
    beta, *_ = np.linalg.lstsq(x, r.to_numpy(float), rcond=None)
    alphas = beta[0]
    resid = r.to_numpy(float) - x @ beta
    sigma = resid.T @ resid / (t - k - 1)

    mu_f = f.mean().to_numpy(float)
    omega = np.cov(f.to_numpy(float).T, ddof=1).reshape(k, k)
    sharpe_sq = float(mu_f @ np.linalg.pinv(omega) @ mu_f)
    stat = (t - n - k) / n * (alphas @ np.linalg.pinv(sigma) @ alphas) / (1 + sharpe_sq)
    p = 1 - stats.f.cdf(stat, n, t - n - k)
    return {
        "grs_statistic": float(stat),
        "p_value": float(p),
        "n_portfolios": float(n),
        "n_periods": float(t),
        "mean_abs_alpha": float(np.abs(alphas).mean()),
    }
