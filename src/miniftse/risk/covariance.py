"""Covariance estimation.

The sample covariance matrix is the maximum-likelihood estimate and it is nearly
useless for portfolio construction. With N assets and T observations you are estimating
N(N+1)/2 parameters from NT numbers: at N=500 and T=252 that is 125,250 parameters from
126,000 observations, and the matrix is singular.

The damage is specific rather than diffuse. The sample matrix systematically
*understates* the smallest eigenvalues, and a minimum-variance optimiser goes looking
for exactly those directions - so it loads up on the linear combinations whose risk has
been estimated worst. The optimiser is an error maximiser, and the covariance matrix is
where most of the error lives.

Three fixes, in increasing order of structure imposed:

* **Shrinkage** pulls the sample matrix toward a low-parameter target.
* **EWMA** weights recent observations more, trading estimation error for adaptiveness.
* **A factor model** imposes an explicit structure, which is what `factor_model` does.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class CovarianceEstimate:
    matrix: pd.DataFrame
    method: str
    n_obs: int
    shrinkage_intensity: float = 0.0
    condition_number: float = 0.0
    notes: str = ""

    @property
    def volatilities(self) -> pd.Series:
        return pd.Series(np.sqrt(np.diag(self.matrix)), index=self.matrix.index)

    def correlation(self) -> pd.DataFrame:
        vol = self.volatilities.to_numpy()
        outer = np.outer(vol, vol)
        with np.errstate(divide="ignore", invalid="ignore"):
            corr = np.where(outer > 0, self.matrix.to_numpy() / outer, 0.0)
        return pd.DataFrame(corr, index=self.matrix.index, columns=self.matrix.columns)

    def portfolio_variance(self, weights: pd.Series) -> float:
        w = weights.reindex(self.matrix.index).fillna(0.0).to_numpy()
        return float(w @ self.matrix.to_numpy() @ w)

    def portfolio_volatility(self, weights: pd.Series, annualise: bool = True) -> float:
        var = max(self.portfolio_variance(weights), 0.0)
        return float(np.sqrt(var) * (np.sqrt(252) if annualise else 1.0))

    def is_positive_definite(self) -> bool:
        try:
            np.linalg.cholesky(self.matrix.to_numpy())
            return True
        except np.linalg.LinAlgError:
            return False


def _prepare(returns: pd.DataFrame, min_obs: int = 60) -> pd.DataFrame:
    """Keep securities with enough history, and fill the rest of the gaps with zero.

    Zero-filling is a choice with a cost: it biases correlations toward zero for names
    with sparse history. The alternative - dropping any name with a single gap -
    systematically excludes recent listings and anything that has ever been suspended,
    which is a worse bias because it is correlated with the characteristics being
    studied.
    """
    counts = returns.notna().sum()
    keep = counts[counts >= min_obs].index
    return returns[keep].fillna(0.0)


def sample_covariance(returns: pd.DataFrame, min_obs: int = 60) -> CovarianceEstimate:
    """The textbook estimator. Included as the baseline everything else has to beat."""
    r = _prepare(returns, min_obs)
    cov = r.cov(ddof=1)
    eig = np.linalg.eigvalsh(cov.to_numpy())
    cond = float(eig.max() / eig.min()) if eig.min() > 0 else float("inf")
    return CovarianceEstimate(
        matrix=cov, method="sample", n_obs=len(r), condition_number=cond,
        notes=(
            f"{r.shape[1]} assets from {len(r)} observations; "
            + ("singular or near-singular - N exceeds T"
               if r.shape[1] >= len(r)
               else "full rank but ill-conditioned" if cond > 1e4 else "well conditioned")
        ),
    )


def ledoit_wolf(returns: pd.DataFrame, min_obs: int = 60) -> CovarianceEstimate:
    """Ledoit-Wolf shrinkage toward a constant-correlation target.

    Convex combination of the sample matrix and a structured target::

        Sigma = delta * F + (1 - delta) * S

    The target `F` keeps each asset's own variance but replaces every pairwise
    correlation with the average - N parameters instead of N(N+1)/2. Biased and far
    lower variance.

    The intensity `delta` is chosen analytically to minimise expected squared error, so
    there is no parameter to tune and no opportunity to tune one until the backtest
    improves. That absence is a feature.
    """
    r = _prepare(returns, min_obs)
    x = r.to_numpy(dtype=float)
    t, n = x.shape
    if t < 2 or n < 2:
        raise ValueError("need at least two assets and two observations")

    x = x - x.mean(axis=0)
    sample = x.T @ x / t

    vols = np.sqrt(np.diag(sample))
    outer = np.outer(vols, vols)
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.where(outer > 0, sample / outer, 0.0)
    off_diagonal = corr[~np.eye(n, dtype=bool)]
    mean_corr = float(off_diagonal.mean()) if off_diagonal.size else 0.0

    target = mean_corr * outer
    np.fill_diagonal(target, np.diag(sample))

    # pi: sum of asymptotic variances of the sample covariance entries.
    x2 = x**2
    phi_mat = (x2.T @ x2) / t - sample**2
    pi = float(phi_mat.sum())

    # rho: covariance between the sample entries and the target entries.
    term = np.zeros((n, n))
    for i in range(n):
        yi = x[:, i]
        term[i, :] = ((yi**2)[:, None] * x).T @ yi / t - sample[i, :] * sample[i, i]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(vols > 0, 1.0 / vols, 0.0)
    rho_off = mean_corr * (
        (np.outer(vols, ratio) * term + np.outer(ratio, vols) * term.T) / 2.0
    )
    np.fill_diagonal(rho_off, 0.0)
    rho = float(np.trace(phi_mat) + rho_off.sum())

    gamma = float(((target - sample) ** 2).sum())
    kappa = (pi - rho) / gamma if gamma > 0 else 0.0
    delta = float(np.clip(kappa / t, 0.0, 1.0))

    shrunk = delta * target + (1 - delta) * sample
    eig = np.linalg.eigvalsh(shrunk)
    cond = float(eig.max() / eig.min()) if eig.min() > 0 else float("inf")

    return CovarianceEstimate(
        matrix=pd.DataFrame(shrunk, index=r.columns, columns=r.columns),
        method="ledoit-wolf", n_obs=t, shrinkage_intensity=delta,
        condition_number=cond,
        notes=(
            f"shrunk {delta:.1%} toward a constant-correlation target with mean "
            f"correlation {mean_corr:.3f}"
        ),
    )


def ewma_covariance(
    returns: pd.DataFrame, half_life: int = 90, min_obs: int = 60
) -> CovarianceEstimate:
    """Exponentially weighted covariance.

    Adapts to volatility regimes, which matters because equity correlations rise
    sharply in a crisis and a one-year equal-weighted window is still averaging in the
    calm months. The half-life is a real trade-off: shorter reacts faster and estimates
    worse, and RiskMetrics' 90-day-ish default is a reasonable compromise rather than
    a discovered optimum.
    """
    r = _prepare(returns, min_obs)
    x = r.to_numpy(dtype=float)
    t = len(x)
    lam = 0.5 ** (1.0 / half_life)
    weights = lam ** np.arange(t - 1, -1, -1)
    weights = weights / weights.sum()

    mean = weights @ x
    centred = x - mean
    cov = (centred * weights[:, None]).T @ centred / (1 - (weights**2).sum())

    eig = np.linalg.eigvalsh(cov)
    return CovarianceEstimate(
        matrix=pd.DataFrame(cov, index=r.columns, columns=r.columns),
        method=f"ewma(half_life={half_life})", n_obs=t,
        condition_number=float(eig.max() / eig.min()) if eig.min() > 0 else float("inf"),
        notes=f"effective sample size {1 / (weights**2).sum():.0f} of {t} observations",
    )


def pca_covariance(
    returns: pd.DataFrame, n_components: int = 10, min_obs: int = 60
) -> CovarianceEstimate:
    """Statistical factor model: keep the top principal components, diagonalise the rest.

    Fits the data better than a fundamental model by construction - it is the optimal
    low-rank approximation. Its weakness is interpretation: the third principal
    component has no name, so a risk report built on it cannot tell a client *why* the
    portfolio is risky. That is why fundamental factor models dominate in practice even
    though they explain less variance.
    """
    r = _prepare(returns, min_obs)
    x = r.to_numpy(dtype=float)
    x = x - x.mean(axis=0)
    cov = x.T @ x / (len(x) - 1)

    values, vectors = np.linalg.eigh(cov)
    order = np.argsort(values)[::-1]
    values, vectors = values[order], vectors[:, order]

    k = min(n_components, len(values))
    loadings = vectors[:, :k] * np.sqrt(np.clip(values[:k], 0, None))
    systematic = loadings @ loadings.T
    specific = np.clip(np.diag(cov) - np.diag(systematic), 1e-12, None)
    reconstructed = systematic + np.diag(specific)

    explained = float(values[:k].sum() / values.sum()) if values.sum() > 0 else 0.0
    return CovarianceEstimate(
        matrix=pd.DataFrame(reconstructed, index=r.columns, columns=r.columns),
        method=f"pca({k})", n_obs=len(x),
        condition_number=float(np.linalg.cond(reconstructed)),
        notes=f"{k} components explain {explained:.1%} of total variance",
    )


# --------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------


def bias_test(
    predicted_vol: pd.Series, realised_returns: pd.Series
) -> dict[str, float]:
    """The standard test of whether a risk model is honest.

    Standardise each realised return by the volatility the model forecast for that
    period. If the model is right the standardised series has unit standard deviation -
    the *bias statistic*.

    * above 1: the model under-forecasts risk, which is the common failure and the
      dangerous one, because it shows up in a crisis
    * below 1: over-forecasts, which costs return through unnecessary caution

    The 95% confidence interval is roughly ``1 +/- sqrt(2/T)``, so with 250 observations
    anything outside about [0.91, 1.09] is a real problem rather than noise.
    """
    frame = pd.concat(
        [predicted_vol.rename("pred"), realised_returns.rename("real")], axis=1
    ).dropna()
    frame = frame[frame["pred"] > 0]
    if len(frame) < 20:
        return {"bias_statistic": float("nan"), "n_obs": float(len(frame))}

    standardised = frame["real"] / frame["pred"]
    t = len(frame)
    bias = float(standardised.std(ddof=1))
    ci = float(np.sqrt(2.0 / t))
    return {
        "bias_statistic": bias,
        "n_obs": float(t),
        "ci_lower": 1 - 2 * ci,
        "ci_upper": 1 + 2 * ci,
        "within_ci": float(1 - 2 * ci <= bias <= 1 + 2 * ci),
        "mean_standardised": float(standardised.mean()),
        "kurtosis": float(standardised.kurtosis()),
        "interpretation": 0.0 if abs(bias - 1) < ci else (1.0 if bias > 1 else -1.0),
    }


def estimator_bakeoff(
    returns: pd.DataFrame,
    estimation_window: int = 252,
    holding_period: int = 21,
    min_obs: int = 60,
) -> pd.DataFrame:
    """Walk-forward comparison of the estimators on out-of-sample minimum-variance risk.

    The evaluation that matters: build a minimum-variance portfolio from each estimator
    using only past data, hold it, and measure what actually happened. The sample
    covariance reliably wins in-sample and loses out-of-sample, which is the whole
    lesson of this module in one table.
    """
    estimators = {
        "sample": lambda r: sample_covariance(r, min_obs),
        "ledoit_wolf": lambda r: ledoit_wolf(r, min_obs),
        "ewma_90": lambda r: ewma_covariance(r, 90, min_obs),
        "pca_10": lambda r: pca_covariance(r, 10, min_obs),
    }
    rows: list[dict[str, float | str]] = []
    starts = range(estimation_window, len(returns) - holding_period, holding_period)

    for name, build in estimators.items():
        realised: list[float] = []
        predicted: list[float] = []
        for start in starts:
            train = returns.iloc[start - estimation_window: start]
            test = returns.iloc[start: start + holding_period]
            try:
                est = build(train)
                w = minimum_variance_weights(est)
            except (ValueError, np.linalg.LinAlgError):
                continue
            common = [c for c in w.index if c in test.columns]
            if not common:
                continue
            port = (test[common].fillna(0.0) * w[common]).sum(axis=1)
            realised.append(float(port.std() * np.sqrt(252)))
            predicted.append(est.portfolio_volatility(w))

        if not realised:
            continue
        realised_arr, predicted_arr = np.array(realised), np.array(predicted)
        rows.append({
            "estimator": name,
            "mean_realised_vol": float(realised_arr.mean()),
            "mean_predicted_vol": float(predicted_arr.mean()),
            "bias_ratio": float((realised_arr / predicted_arr).mean()),
            "n_windows": float(len(realised)),
        })
    return pd.DataFrame(rows).sort_values("mean_realised_vol").reset_index(drop=True)


def minimum_variance_weights(
    estimate: CovarianceEstimate, long_only: bool = True, max_weight: float = 0.10
) -> pd.Series:
    """Analytical minimum-variance weights, with an optional long-only projection.

    The closed form ``w = Sigma^-1 * 1 / (1' Sigma^-1 * 1)`` is unconstrained and
    routinely produces large short positions in whichever assets the estimator happened
    to think were most negatively correlated - which is precisely where the estimation
    error is. The long-only projection here is a crude clip-and-renormalise; the proper
    treatment is in `optim`.
    """
    sigma = estimate.matrix.to_numpy()
    n = len(sigma)
    ones = np.ones(n)
    try:
        inv = np.linalg.inv(sigma + np.eye(n) * 1e-10)
    except np.linalg.LinAlgError:
        inv = np.linalg.pinv(sigma)
    w = inv @ ones
    denom = ones @ w
    w = w / denom if abs(denom) > 1e-12 else ones / n

    if long_only:
        w = np.clip(w, 0.0, max_weight)
        total = w.sum()
        w = w / total if total > 0 else ones / n
    return pd.Series(w, index=estimate.matrix.index)
