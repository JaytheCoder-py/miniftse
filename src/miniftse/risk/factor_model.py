"""A Barra-style fundamental factor risk model.

    Sigma = B F B' + D

* ``B`` exposures, N x K, **observed** rather than estimated - a stock's book-to-price
  is a fact about the stock
* ``F`` factor covariance, K x K, estimated from the factor return series
* ``D`` specific variance, diagonal

The parameter count is the point. A 500-asset sample covariance needs 125,250
parameters; this needs K(K+1)/2 for F plus N for D - about 800 at K=25. That is the
difference between a matrix that is singular and one that is usable.

Why *fundamental* rather than statistical, given that PCA fits better by construction:
the exposures are interpretable. "Your tracking error is 2.1%, of which 0.8% is a value
tilt and 0.6% is an overweight to technology" is a sentence a client can act on.
"0.8% comes from the third principal component" is not. At an index provider, where
every risk number ends up in front of someone external, that decides the choice.

Factor returns are estimated by weighted cross-sectional regression each period - the
same Fama-MacBeth machinery, reused with the roles swapped: there the coefficients were
premia to be tested, here they are factor returns whose covariance is the model.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from miniftse.risk.covariance import bias_test


@dataclass
class FactorReturns:
    """Estimated factor returns and the regression diagnostics behind them."""

    returns: pd.DataFrame
    r_squared: pd.Series
    n_obs: pd.Series
    residuals: pd.DataFrame

    def summary(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "mean_daily": self.returns.mean(),
                "annualised": self.returns.mean() * 252,
                "vol_annualised": self.returns.std() * np.sqrt(252),
                "t_stat": self.returns.mean() / (self.returns.std() / np.sqrt(len(self.returns))),
            }
        ).round(4)


@dataclass
class RiskModel:
    """An estimated `Sigma = B F B' + D`."""

    exposures: pd.DataFrame
    factor_covariance: pd.DataFrame
    specific_variance: pd.Series
    factor_returns: FactorReturns
    estimation_date: object = None
    half_life: int = 90

    @property
    def factors(self) -> list[str]:
        return list(self.factor_covariance.index)

    @property
    def securities(self) -> list[str]:
        return list(self.exposures.index)

    def covariance(self, securities: list[str] | None = None) -> pd.DataFrame:
        """Materialise the full asset covariance.

        Rarely needed and deliberately not the default path. The model's advantage is
        that portfolio risk can be computed as ``(B'w)' F (B'w) + w'Dw`` without ever
        forming an N x N matrix, which is what makes it tractable at N in the thousands.
        """
        ids = securities or self.securities
        b = self.exposures.reindex(ids).fillna(0.0)
        systematic = b.to_numpy() @ self.factor_covariance.to_numpy() @ b.to_numpy().T
        total = systematic + np.diag(
            self.specific_variance.reindex(ids)
            .fillna(float(self.specific_variance.median()))
            .to_numpy()
        )
        return pd.DataFrame(total, index=ids, columns=ids)

    def portfolio_exposures(self, weights: pd.Series) -> pd.Series:
        """`B'w` - the portfolio's factor loadings. The headline risk report line."""
        w = weights.reindex(self.exposures.index).fillna(0.0)
        return self.exposures.T @ w

    def portfolio_variance(self, weights: pd.Series) -> float:
        w = weights.reindex(self.exposures.index).fillna(0.0)
        x = self.portfolio_exposures(w).to_numpy()
        factor_var = float(x @ self.factor_covariance.to_numpy() @ x)
        specific = self.specific_variance.reindex(w.index).fillna(
            float(self.specific_variance.median())
        )
        specific_var = float((w.to_numpy() ** 2 * specific.to_numpy()).sum())
        return factor_var + specific_var

    def portfolio_volatility(self, weights: pd.Series, annualise: bool = True) -> float:
        return float(
            np.sqrt(max(self.portfolio_variance(weights), 0.0))
            * (np.sqrt(252) if annualise else 1.0)
        )

    def tracking_error(
        self, weights: pd.Series, benchmark: pd.Series, annualise: bool = True
    ) -> float:
        """Ex-ante active risk: the volatility of the active weight vector."""
        ids = self.exposures.index
        active = weights.reindex(ids).fillna(0.0) - benchmark.reindex(ids).fillna(0.0)
        return self.portfolio_volatility(active, annualise)

    # ------------------------------------------------------------------ decomposition

    def risk_decomposition(
        self, weights: pd.Series, benchmark: pd.Series | None = None
    ) -> pd.DataFrame:
        """Split risk into per-factor contributions plus specific.

        Contributions use the marginal-contribution identity, so they sum exactly to
        total variance. That exactness is the acceptance test: a decomposition whose
        parts do not add up is not a decomposition, and a client will check.
        """
        ids = self.exposures.index
        w = weights.reindex(ids).fillna(0.0)
        if benchmark is not None:
            w = w - benchmark.reindex(ids).fillna(0.0)

        x = self.portfolio_exposures(w)
        f = self.factor_covariance
        fx = f.to_numpy() @ x.to_numpy()
        factor_contributions = x.to_numpy() * fx  # x_k * (F x)_k, sums to x'Fx

        specific = self.specific_variance.reindex(ids).fillna(
            float(self.specific_variance.median())
        )
        specific_var = float((w.to_numpy() ** 2 * specific.to_numpy()).sum())
        total_var = float(factor_contributions.sum()) + specific_var

        rows = [
            {
                "source": factor,
                "type": "factor",
                "exposure": float(x[factor]),
                "variance_contribution": float(contribution),
                "pct_of_total": float(contribution / total_var) if total_var else 0.0,
            }
            for factor, contribution in zip(f.index, factor_contributions, strict=False)
        ]
        rows.append(
            {
                "source": "specific",
                "type": "specific",
                "exposure": float("nan"),
                "variance_contribution": specific_var,
                "pct_of_total": specific_var / total_var if total_var else 0.0,
            }
        )

        frame = pd.DataFrame(rows)
        frame["risk_contribution_annualised"] = (
            frame["variance_contribution"] / np.sqrt(max(total_var, 1e-18)) * np.sqrt(252)
        )
        return frame.sort_values("variance_contribution", ascending=False).reset_index(drop=True)

    def marginal_contributions(
        self, weights: pd.Series, benchmark: pd.Series | None = None
    ) -> pd.Series:
        """d(sigma)/d(w_i): how much risk one more unit of each name adds.

        The optimiser's view of the portfolio. At an optimum, marginal contribution is
        proportional to expected return - so a name with high marginal risk and no
        expected return is the first thing to cut.
        """
        ids = self.exposures.index
        w = weights.reindex(ids).fillna(0.0)
        if benchmark is not None:
            w = w - benchmark.reindex(ids).fillna(0.0)

        b = self.exposures.to_numpy()
        fx = self.factor_covariance.to_numpy() @ (b.T @ w.to_numpy())
        specific = (
            self.specific_variance.reindex(ids)
            .fillna(float(self.specific_variance.median()))
            .to_numpy()
        )
        cov_w = b @ fx + specific * w.to_numpy()
        vol = np.sqrt(max(float(w.to_numpy() @ cov_w), 1e-18))
        return pd.Series(cov_w / vol * np.sqrt(252), index=ids)

    def run_bias_test(
        self, weights: pd.Series, realised: pd.Series, window: int = 21
    ) -> dict[str, float]:
        """Bias test for a fixed weight vector against realised returns."""
        predicted = pd.Series(
            self.portfolio_volatility(weights, annualise=False), index=realised.index
        )
        del window
        return bias_test(predicted, realised)


# --------------------------------------------------------------------------------------
# Estimation
# --------------------------------------------------------------------------------------


@dataclass
class FactorModelEstimator:
    """Estimates a fundamental factor model from exposures and returns."""

    factor_half_life: int = 90
    specific_half_life: int = 60
    newey_west_lags: int = 2
    """Serial correlation in daily factor returns - from non-synchronous trading across
    time zones and from stale prices - biases the factor covariance downward. A
    Newey-West adjustment on the factor covariance corrects it, and without it the model
    under-forecasts risk for exactly the global portfolios it is meant to serve."""

    specific_shrinkage: float = 0.25
    """Specific variances are estimated from one asset's residuals each, so they are
    noisy. Shrinking toward the cross-sectional median is a cheap, effective Bayesian
    correction."""

    min_securities: int = 30

    def estimate_factor_returns(
        self,
        returns: pd.DataFrame,
        exposures: dict[object, pd.DataFrame],
        weights: pd.DataFrame | None = None,
    ) -> FactorReturns:
        """Weighted cross-sectional regression of returns on exposures, each period.

        Weighting by square-root market cap is the Barra convention. Unweighted lets
        hundreds of microcaps determine the factor returns that price the mega-caps;
        cap-weighting lets a handful of mega-caps determine everything. Square root
        splits the difference, and being able to say why is the point.
        """
        rows: list[dict[str, float]] = []
        r2s: dict[object, float] = {}
        n_obs: dict[object, int] = {}
        residuals: dict[object, pd.Series] = {}

        for date in returns.index:
            if date not in exposures:
                continue
            b = exposures[date]
            y = returns.loc[date].reindex(b.index)
            frame = pd.concat([y.rename("_y"), b], axis=1).dropna()
            if len(frame) < self.min_securities:
                continue

            yv = frame["_y"].to_numpy(dtype=float)
            xv = frame.drop(columns="_y").to_numpy(dtype=float)
            names = list(frame.drop(columns="_y").columns)

            if weights is not None and date in weights.index:
                w = weights.loc[date].reindex(frame.index).fillna(0.0).to_numpy()
                w = np.sqrt(np.clip(w, 1e-12, None))
            else:
                w = np.ones(len(frame))

            beta, *_ = np.linalg.lstsq(xv * w[:, None], yv * w, rcond=None)
            fitted = xv @ beta
            resid = yv - fitted
            ss_tot = float(((yv - yv.mean()) ** 2).sum())

            rows.append({"date": date, **dict(zip(names, beta, strict=False))})
            r2s[date] = 1.0 - float((resid**2).sum()) / ss_tot if ss_tot > 0 else 0.0
            n_obs[date] = len(frame)
            residuals[date] = pd.Series(resid, index=frame.index)

        if not rows:
            raise ValueError("no period had enough securities to estimate factor returns")

        return FactorReturns(
            returns=pd.DataFrame(rows).set_index("date"),
            r_squared=pd.Series(r2s),
            n_obs=pd.Series(n_obs),
            residuals=pd.DataFrame(residuals).T,
        )

    def factor_covariance(self, factor_returns: pd.DataFrame) -> pd.DataFrame:
        """EWMA covariance of factor returns with a Newey-West serial-correlation term."""
        x = factor_returns.dropna(how="all").fillna(0.0).to_numpy(dtype=float)
        t = len(x)
        if t < 20:
            raise ValueError("need at least 20 periods of factor returns")

        lam = 0.5 ** (1.0 / self.factor_half_life)
        w = lam ** np.arange(t - 1, -1, -1)
        w = w / w.sum()
        mean = w @ x
        centred = x - mean
        cov = (centred * w[:, None]).T @ centred

        for lag in range(1, self.newey_west_lags + 1):
            if lag >= t:
                break
            wl = w[lag:]
            a, b = centred[lag:], centred[:-lag]
            gamma = (a * wl[:, None]).T @ b
            bartlett = 1.0 - lag / (self.newey_west_lags + 1.0)
            cov += bartlett * (gamma + gamma.T)

        cov = _nearest_positive_definite(cov)
        return pd.DataFrame(cov, index=factor_returns.columns, columns=factor_returns.columns)

    def specific_variance(self, residuals: pd.DataFrame) -> pd.Series:
        """EWMA specific variance, shrunk toward the cross-sectional median."""
        t = len(residuals)
        lam = 0.5 ** (1.0 / self.specific_half_life)
        w = lam ** np.arange(t - 1, -1, -1)
        w = w / w.sum()

        sq = residuals.fillna(0.0) ** 2
        var = pd.Series((sq.to_numpy() * w[:, None]).sum(axis=0), index=residuals.columns)
        coverage = residuals.notna().mean()
        var = var.where(coverage > 0.25)

        median = float(var.median()) if var.notna().any() else 1e-6
        shrunk = (1 - self.specific_shrinkage) * var.fillna(
            median
        ) + self.specific_shrinkage * median
        return shrunk.clip(lower=1e-10)

    def fit(
        self,
        returns: pd.DataFrame,
        exposures: dict[object, pd.DataFrame],
        weights: pd.DataFrame | None = None,
        as_of: object = None,
    ) -> RiskModel:
        fr = self.estimate_factor_returns(returns, exposures, weights)
        cov = self.factor_covariance(fr.returns)
        specific = self.specific_variance(fr.residuals)

        last_date = as_of or max(exposures)
        final_exposures = (
            exposures[last_date] if last_date in exposures else exposures[max(exposures)]
        )

        return RiskModel(
            exposures=final_exposures.reindex(columns=cov.index).fillna(0.0),
            factor_covariance=cov,
            specific_variance=specific.reindex(final_exposures.index).fillna(
                float(specific.median())
            ),
            factor_returns=fr,
            estimation_date=last_date,
            half_life=self.factor_half_life,
        )


def build_exposures(
    style_scores: pd.DataFrame,
    industry: pd.Series,
    country: pd.Series | None = None,
    include_market: bool = True,
) -> pd.DataFrame:
    """Assemble the exposure matrix: market + styles + industry dummies.

    The collinearity trap: industry dummies sum to one for every security, so together
    with a market column of ones the matrix is singular. Barra's answer is a
    cap-weighted constraint that industry factor returns sum to zero; the simpler
    equivalent used here is to drop one industry, making the remaining industry factors
    read as *relative to* the omitted one. Either works; failing to do either produces
    a rank-deficient regression whose coefficients are arbitrary.
    """
    blocks: list[pd.DataFrame] = []
    if include_market:
        blocks.append(pd.DataFrame({"market": 1.0}, index=style_scores.index))
    blocks.append(style_scores)

    dummies = pd.get_dummies(industry.reindex(style_scores.index), prefix="ind", dtype=float)
    if include_market and dummies.shape[1] > 1:
        dummies = dummies.iloc[:, 1:]  # drop one to break collinearity with `market`
    blocks.append(dummies)

    if country is not None:
        cty = pd.get_dummies(country.reindex(style_scores.index), prefix="cty", dtype=float)
        if cty.shape[1] > 1:
            cty = cty.iloc[:, 1:]
        blocks.append(cty)

    return pd.concat(blocks, axis=1).fillna(0.0)


def _nearest_positive_definite(matrix: np.ndarray, epsilon: float = 1e-12) -> np.ndarray:
    """Clip negative eigenvalues to a small positive floor.

    The Newey-West adjustment can push the covariance matrix indefinite, and an
    indefinite covariance makes the optimiser report negative variance - which it will
    then happily minimise toward minus infinity.
    """
    sym = (matrix + matrix.T) / 2.0
    values, vectors = np.linalg.eigh(sym)
    if values.min() > epsilon:
        return sym
    values = np.clip(values, epsilon, None)
    return vectors @ np.diag(values) @ vectors.T
