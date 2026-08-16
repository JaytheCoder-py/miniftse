"""Cross-sectional signal processing: winsorise, standardise, neutralise, combine.

The order of operations is not cosmetic and is the most common source of quiet
disagreement between two implementations of "the same" factor:

1. **Winsorise** before standardising. Standardising first lets one outlier inflate the
   standard deviation, which compresses everyone else toward zero.
2. **Standardise within region** before neutralising to industry. Cross-region z-scores
   mix currencies, accounting regimes and market levels.
3. **Neutralise last**, by regression rather than by subtracting group means, so several
   neutralisation targets can be applied at once without fighting each other.
4. **Fill missing values after neutralising**, at the neutral value, so a company with no
   data is neither rewarded nor punished for it.

Every step is a published methodology decision. `FactorPipeline` records the ones it
made so the document and the code can be checked against each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


class MissingDataPolicy:
    """How to treat a security with no value for a signal.

    Not a detail. Missing data is not random - it clusters in small caps, recent
    listings and emerging markets - so any policy that quietly drops or imputes creates
    a systematic tilt. Stating the policy explicitly is the only defensible option.
    """

    NEUTRAL = "neutral"
    """Assign the cross-sectional mean, i.e. zero after standardising. Conservative:
    a name with no data gets no view."""

    EXCLUDE = "exclude"
    """Remove from the factor entirely. Honest, but shrinks the universe and can
    introduce the very bias it is meant to avoid."""

    WORST = "worst"
    """Assign the worst decile. Defensible only where absence is itself information -
    a company that does not disclose emissions, for instance."""

    INDUSTRY_MEDIAN = "industry_median"
    """Impute the industry median. Reasonable for ratios that are strongly industry-
    determined; wrong for anything idiosyncratic."""


def winsorise(
    values: pd.Series, lower: float = 0.01, upper: float = 0.99, by_mad: bool = False
) -> pd.Series:
    """Clip the tails.

    Percentile clipping by default. `by_mad=True` clips at a number of median absolute
    deviations instead, which is more robust when the distribution is very skewed - as
    book-to-price is, because a handful of names have near-zero market cap and produce
    ratios in the hundreds.
    """
    s = values.dropna()
    if s.empty:
        return values
    if by_mad:
        median = s.median()
        mad = (s - median).abs().median()
        if mad == 0:
            return values
        scale = 1.4826 * mad  # consistency factor for a normal distribution
        return values.clip(median - 3 * scale, median + 3 * scale)
    return values.clip(s.quantile(lower), s.quantile(upper))


def zscore(values: pd.Series, weights: pd.Series | None = None) -> pd.Series:
    """Standardise to mean zero, unit standard deviation.

    `weights` gives a cap-weighted mean, which is what you want when the factor is going
    to be applied as a tilt to a cap-weighted index: it makes the parent index score
    zero by construction, so the tilt has no unintended net exposure.
    """
    s = values.astype(float)
    valid = s.notna()
    if valid.sum() < 2:
        return pd.Series(0.0, index=values.index)
    if weights is not None:
        w = weights.reindex(s.index).fillna(0.0)
        w = w.where(valid, 0.0)
        if w.sum() <= 0:
            mean = s[valid].mean()
        else:
            mean = float((s[valid] * w[valid]).sum() / w[valid].sum())
    else:
        mean = float(s[valid].mean())
    sd = float(s[valid].std(ddof=1))
    if sd == 0 or not np.isfinite(sd):
        return pd.Series(0.0, index=values.index)
    return (s - mean) / sd


def rank_normalise(values: pd.Series) -> pd.Series:
    """Map to a standard normal by rank.

    Discards magnitude and keeps order. More robust than a z-score and the right choice
    when the raw distribution is badly behaved, at the cost of throwing away genuine
    information about how far apart two names are.
    """
    s = values.dropna()
    if s.empty:
        return pd.Series(0.0, index=values.index)
    from scipy.stats import norm

    ranks = s.rank(method="average") / (len(s) + 1)
    out = pd.Series(norm.ppf(ranks), index=s.index)
    return out.reindex(values.index)


def neutralise(
    values: pd.Series,
    dummies: pd.DataFrame | None = None,
    controls: pd.DataFrame | None = None,
    weights: pd.Series | None = None,
) -> pd.Series:
    """Regress out group membership and continuous controls; keep the residual.

    Regression rather than group-mean subtraction, because that generalises: industry
    dummies, country dummies and a continuous size control can be removed
    simultaneously and consistently. Subtracting means one group at a time gives a
    different answer depending on the order.

    The usual case is industry-neutralising a value factor. Without it, "cheap" mostly
    means "a bank", and the factor becomes a sector bet wearing a factor's clothes.
    """
    y = values.astype(float)
    valid = y.notna()
    if valid.sum() < 3:
        return y

    blocks: list[pd.DataFrame] = []
    if dummies is not None and not dummies.empty:
        blocks.append(dummies.reindex(y.index).fillna(0.0))
    if controls is not None and not controls.empty:
        blocks.append(controls.reindex(y.index).astype(float).fillna(0.0))
    if not blocks:
        return y

    x = pd.concat(blocks, axis=1)
    x = x.loc[:, x.std() > 0]
    if x.empty:
        return y
    x = x.assign(_const=1.0)

    xv = x[valid].to_numpy(dtype=float)
    yv = y[valid].to_numpy(dtype=float)
    if weights is not None:
        w = np.sqrt(weights.reindex(y.index).fillna(0.0)[valid].to_numpy(dtype=float))
        w = np.where(w > 0, w, 1e-8)
        xv, yv = xv * w[:, None], yv * w
    beta, *_ = np.linalg.lstsq(xv, yv, rcond=None)

    fitted = x[valid].to_numpy(dtype=float) @ beta
    out = pd.Series(np.nan, index=y.index)
    out[valid] = y[valid].to_numpy(dtype=float) - fitted
    return out


def fill_missing(
    values: pd.Series,
    policy: str = MissingDataPolicy.NEUTRAL,
    groups: pd.Series | None = None,
) -> pd.Series:
    match policy:
        case MissingDataPolicy.NEUTRAL:
            return values.fillna(0.0)
        case MissingDataPolicy.EXCLUDE:
            return values.dropna()
        case MissingDataPolicy.WORST:
            floor = values.dropna().quantile(0.05) if values.notna().any() else 0.0
            return values.fillna(floor)
        case MissingDataPolicy.INDUSTRY_MEDIAN:
            if groups is None:
                return values.fillna(values.median())
            med = values.groupby(groups).transform("median")
            return values.fillna(med).fillna(values.median()).fillna(0.0)
        case _:
            raise ValueError(f"unknown missing-data policy {policy!r}")


# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineSpec:
    """A publishable description of how a raw signal becomes a factor score."""

    winsorise_lower: float = 0.01
    winsorise_upper: float = 0.99
    use_mad: bool = False
    standardise: str = "zscore"
    """'zscore' or 'rank'."""

    cap_weighted_mean: bool = True
    neutralise_industry: bool = True
    neutralise_country: bool = False
    neutralise_size: bool = False
    missing_policy: str = MissingDataPolicy.NEUTRAL
    restandardise_after_neutralise: bool = True

    def describe(self) -> str:
        """Prose for the methodology document, generated from the spec so the two
        cannot disagree."""
        parts = [
            f"Raw values are winsorised at the "
            f"{self.winsorise_lower:.0%}/{self.winsorise_upper:.0%} percentiles"
            + (" using a median-absolute-deviation rule" if self.use_mad else "")
            + ".",
            "They are then standardised by "
            + ("rank-normalisation" if self.standardise == "rank" else "z-score")
            + (
                ", using a capitalisation-weighted mean so that the parent index scores zero"
                if self.cap_weighted_mean
                else ""
            )
            + ".",
        ]
        targets = [
            n
            for n, on in (
                ("industry", self.neutralise_industry),
                ("country", self.neutralise_country),
                ("size", self.neutralise_size),
            )
            if on
        ]
        if targets:
            parts.append(
                f"Scores are neutralised to {', '.join(targets)} by cross-sectional "
                "regression, and the residual is retained."
            )
        parts.append(
            {
                MissingDataPolicy.NEUTRAL: "Securities with no value receive the "
                "neutral score of zero.",
                MissingDataPolicy.EXCLUDE: "Securities with no value are excluded.",
                MissingDataPolicy.WORST: "Securities with no value receive the fifth "
                "percentile score.",
                MissingDataPolicy.INDUSTRY_MEDIAN: "Securities with no value receive "
                "their industry median.",
            }[self.missing_policy]
        )
        return " ".join(parts)


@dataclass
class FactorPipeline:
    """Applies a `PipelineSpec` to a cross-section."""

    spec: PipelineSpec = field(default_factory=PipelineSpec)

    def transform(
        self,
        raw: pd.Series,
        industry: pd.Series | None = None,
        country: pd.Series | None = None,
        market_cap: pd.Series | None = None,
    ) -> pd.Series:
        s = winsorise(raw, self.spec.winsorise_lower, self.spec.winsorise_upper, self.spec.use_mad)

        weights = market_cap if self.spec.cap_weighted_mean else None
        s = rank_normalise(s) if self.spec.standardise == "rank" else zscore(s, weights)

        dummies = []
        if self.spec.neutralise_industry and industry is not None:
            dummies.append(pd.get_dummies(industry.reindex(s.index), prefix="ind", dtype=float))
        if self.spec.neutralise_country and country is not None:
            dummies.append(pd.get_dummies(country.reindex(s.index), prefix="cty", dtype=float))
        dummy_block = pd.concat(dummies, axis=1) if dummies else None

        controls = None
        if self.spec.neutralise_size and market_cap is not None:
            controls = pd.DataFrame(
                {"log_size": np.log(market_cap.reindex(s.index).clip(lower=1.0))}
            )

        if dummy_block is not None or controls is not None:
            s = neutralise(s, dummy_block, controls, weights)
            if self.spec.restandardise_after_neutralise:
                s = zscore(s, weights)

        return fill_missing(s, self.spec.missing_policy, industry)

    def transform_panel(
        self,
        raw: pd.DataFrame,
        industry: pd.DataFrame | pd.Series | None = None,
        country: pd.DataFrame | pd.Series | None = None,
        market_cap: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """Apply date by date. A panel-wide transform would leak across time."""
        out = {}
        for date in raw.index:
            ind = industry.loc[date] if isinstance(industry, pd.DataFrame) else industry
            cty = country.loc[date] if isinstance(country, pd.DataFrame) else country
            cap = market_cap.loc[date] if market_cap is not None else None
            out[date] = self.transform(raw.loc[date], ind, cty, cap)
        return pd.DataFrame(out).T


def combine_scores(
    scores: dict[str, pd.Series],
    weights: dict[str, float] | None = None,
    method: str = "integrated",
) -> pd.Series:
    """Combine sub-factors into a composite.

    Two schools, and the choice is a genuine product decision:

    * **integrated** - average the standardised scores, then select or tilt once. A name
      must be good on the blend. Higher exposure per unit of turnover, and the usual
      answer on the numbers.
    * **mixed** (portfolio-of-sleeves) - build one sub-portfolio per factor and average
      the *weights*. Lower combined exposure, because a name that is excellent on value
      and terrible on quality survives in the value sleeve. Its advantage is
      attribution: a client can be shown which sleeve did what, and that
      explainability is often what wins the mandate.
    """
    if not scores:
        raise ValueError("no scores to combine")
    weights = weights or dict.fromkeys(scores, 1.0 / len(scores))
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("combination weights must be positive")

    index = scores[next(iter(scores))].index
    if method == "integrated":
        acc = pd.Series(0.0, index=index)
        for name, s in scores.items():
            acc = acc.add(s.reindex(index).fillna(0.0) * weights[name] / total, fill_value=0.0)
        return acc
    if method == "mixed":
        # Convert each score to a within-sleeve weight, then average the weights.
        sleeves = []
        for name, s in scores.items():
            r = s.reindex(index).rank(pct=True).fillna(0.5)
            sleeve = r / r.sum()
            sleeves.append(sleeve * weights[name] / total)
        combined = sum(sleeves)
        return (combined - combined.mean()) / combined.std(ddof=1)
    raise ValueError(f"unknown combination method {method!r}")
