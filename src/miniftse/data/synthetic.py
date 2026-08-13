"""A deterministic synthetic equity universe.

Why this exists, rather than pointing the engine at a real feed:

* **A clean clone must build a full index history** with no API keys, no network and no
  vendor licence. That is the acceptance criterion for the whole project.
* **Golden-master regression tests need bit-identical inputs.** Real data is revised;
  yesterday's Yahoo response is not today's. You cannot pin an index history to a hash
  if the source drifts underneath you.
* **Pathologies can be placed deliberately.** Real data contains spin-offs and rights
  issues where it happens to contain them. Here they are placed on known dates so the
  corporate action engine can be tested against hand-computed values.

**What this is not.** It is a simulation, and in it value and quality genuinely predict
returns because they were built to. No result computed on this universe is evidence
about real markets. It exercises machinery; it does not discover anything. Every
research output in this repo carries that caveat, and the real-data providers exist so
the same code can be pointed at the genuine article.

Generative model
----------------

    r[i,t] = beta[i]*f_mkt[t] + sum_s B[i,s]*f_s[t] + g[i,ind]*f_ind[t] + eps[i,t]

Style exposures ``B`` are latent, and the fundamentals are generated *from* them, so a
value signal computed off book-to-price recovers a noisy view of the true exposure -
which is exactly the estimation problem factor research actually faces.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass, field, fields
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from miniftse.types import (
    Country,
    Currency,
    IcbIndustry,
    MarketStatus,
    SecurityType,
)

# --------------------------------------------------------------------------------------
# Market definitions
# --------------------------------------------------------------------------------------

#: country -> (currency, market status, mic, weight in universe, dividend frequency)
MARKETS: dict[Country, tuple[Currency, MarketStatus, str, float, int]] = {
    Country.US: (Currency.USD, MarketStatus.DEVELOPED, "XNYS", 0.42, 4),
    Country.GB: (Currency.GBP, MarketStatus.DEVELOPED, "XLON", 0.09, 2),
    Country.JP: (Currency.JPY, MarketStatus.DEVELOPED, "XTKS", 0.10, 2),
    Country.DE: (Currency.EUR, MarketStatus.DEVELOPED, "XETR", 0.06, 1),
    Country.FR: (Currency.EUR, MarketStatus.DEVELOPED, "XPAR", 0.06, 1),
    Country.CH: (Currency.CHF, MarketStatus.DEVELOPED, "XSWX", 0.04, 1),
    Country.CA: (Currency.CAD, MarketStatus.DEVELOPED, "XTSE", 0.05, 4),
    Country.AU: (Currency.AUD, MarketStatus.DEVELOPED, "XASX", 0.04, 2),
    Country.NL: (Currency.EUR, MarketStatus.DEVELOPED, "XAMS", 0.03, 2),
    Country.SE: (Currency.SEK, MarketStatus.DEVELOPED, "XSTO", 0.02, 1),
    Country.HK: (Currency.HKD, MarketStatus.ADVANCED_EMERGING, "XHKG", 0.05, 2),
    Country.KR: (Currency.KRW, MarketStatus.SECONDARY_EMERGING, "XKRX", 0.04, 1),
}

STYLE_FACTORS: tuple[str, ...] = ("value", "quality", "size", "lowvol", "growth")

#: Annualised mean and volatility of each latent style factor's return. The signs
#: encode the simulated world: value and quality pay, growth does not, small pays a
#: little. These are inputs, not findings.
STYLE_PARAMS: dict[str, tuple[float, float]] = {
    "value": (0.030, 0.050),
    "quality": (0.025, 0.038),
    "size": (0.015, 0.055),
    "lowvol": (0.020, 0.040),
    "growth": (-0.010, 0.055),
}

FUNDAMENTAL_ITEMS: tuple[str, ...] = (
    "BOOK_EQUITY",
    "NET_INCOME",
    "REVENUE",
    "TOTAL_ASSETS",
    "TOTAL_DEBT",
    "GROSS_PROFIT",
    "OPERATING_CASHFLOW",
    "CAPEX",
    "DIVIDENDS_PAID",
)


@dataclass(frozen=True, slots=True)
class SyntheticConfig:
    """Every knob. The seed plus this object fully determines the universe."""

    seed: int = 20260809
    n_securities: int = 500
    start: dt.date = dt.date(2015, 1, 1)
    end: dt.date = dt.date(2026, 6, 30)

    base_currency: Currency = Currency.USD
    market_vol: float = 0.135
    market_drift: float = 0.07
    idio_vol_mean: float = 0.24
    idio_vol_disp: float = 0.09
    industry_vol: float = 0.07
    """Calibrated so the reference index lands near 15-17% annualised volatility with a
    worst drawdown in the -30% to -35% region - the range a broad developed-market
    benchmark actually occupies. `market_vol` sits below the target because the
    volatility regime process, the industry factors and the residual style exposure of
    a ~60-name index all add on top."""

    # Corporate action intensities, expressed as expected events per security-year.
    # These are deliberately richer than reality. The universe exists to exercise the
    # corporate action engine, and a realistic split rate would leave whole branches
    # of `corpactions.engine` untouched by the golden master.
    p_split_per_year: float = 0.055
    p_reverse_split_per_year: float = 0.030
    p_rights_issue_per_year: float = 0.012
    p_spinoff_per_year: float = 0.020
    p_special_div_per_year: float = 0.030
    p_buyback_per_year: float = 0.220
    p_float_change_per_year: float = 0.350

    split_price_threshold: float = 90.0
    reverse_split_price_threshold: float = 6.0
    spinoff_min_market_cap: float = 3e9
    frac_mergers_stock: float = 0.35
    """Of terminal merger events, the share this fraction settled in acquirer stock
    rather than cash. Stock mergers are the harder case: the target's index weight
    transfers to the acquirer instead of leaving the index as cash."""

    # Universe churn - without these the panel is survivorship-biased by construction.
    p_delist_per_year: float = 0.022
    frac_late_listings: float = 0.10

    p_restatement: float = 0.06
    p_suspension_per_year: float = 0.010

    fundamental_lag_days: tuple[int, int] = (35, 85)
    restatement_lag_days: tuple[int, int] = (90, 300)

    cache_dir: Path | None = None

    def fingerprint(self) -> str:
        """Stable hash of the configuration, used by the run manifest.

        `cache_dir` is excluded: where the parquet lands does not change the universe,
        and including it would make the same data hash differently on two machines.
        """
        payload = {
            f.name: (
                v.isoformat() if isinstance(v := getattr(self, f.name), dt.date) else v
            )
            for f in fields(self)
            if f.name != "cache_dir"
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------------------


@dataclass
class SyntheticUniverse:
    """A complete market, generated once and served through the provider Protocols."""

    config: SyntheticConfig = field(default_factory=SyntheticConfig)

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.config.seed)
        self._tables: dict[str, pd.DataFrame] = {}

    @property
    def name(self) -> str:
        return f"synthetic:{self.config.fingerprint()}"

    # ---------------------------------------------------------------- calendar

    @cached_property
    def calendar(self) -> pd.DatetimeIndex:
        """Global trading calendar: weekdays less a fixed set of common holidays.

        A single calendar across markets is a simplification. It is the *right* kind of
        simplification: it removes a source of complexity that teaches nothing about
        index construction, while leaving suspensions and delistings - which do - fully
        modelled.
        """
        days = pd.bdate_range(self.config.start, self.config.end)
        holidays = {
            (m, d) for m, d in [(1, 1), (12, 25), (12, 26), (7, 4), (5, 1), (12, 31)]
        }
        return pd.DatetimeIndex([d for d in days if (d.month, d.day) not in holidays])

    @property
    def n_days(self) -> int:
        return len(self.calendar)

    # ---------------------------------------------------------------- reference data

    @cached_property
    def _security_frame(self) -> pd.DataFrame:
        """Static attributes and latent exposures, one row per security."""
        rng = np.random.default_rng(self.config.seed + 1)
        n = self.config.n_securities

        countries = list(MARKETS)
        probs = np.array([MARKETS[c][3] for c in countries], dtype=float)
        probs = probs / probs.sum()
        country_idx = rng.choice(len(countries), size=n, p=probs)

        industries = list(IcbIndustry)
        industry_idx = rng.choice(len(industries), size=n)

        # Log market cap: heavy right tail, so the universe has a genuine mega-cap
        # concentration problem for the capping algorithm to bite on.
        log_cap = rng.normal(21.6, 1.45, size=n)
        log_cap = np.sort(log_cap)[::-1]
        market_cap = np.exp(log_cap)

        # Latent style exposures. Size exposure is mechanically tied to market cap
        # (that is what the size factor means); the rest are drawn with mild
        # correlation, because in reality value and quality are not independent.
        size_expo = -(log_cap - log_cap.mean()) / log_cap.std()
        corr = np.array(
            [
                [1.00, 0.15, 0.00, 0.10, -0.55],
                [0.15, 1.00, 0.00, 0.30, 0.20],
                [0.00, 0.00, 1.00, 0.00, 0.00],
                [0.10, 0.30, 0.00, 1.00, -0.15],
                [-0.55, 0.20, 0.00, -0.15, 1.00],
            ]
        )
        raw = rng.multivariate_normal(np.zeros(5), corr, size=n)
        exposures = {name: raw[:, i] for i, name in enumerate(STYLE_FACTORS)}
        exposures["size"] = size_expo

        # Demean every style exposure by *cap weight*, not equal weight.
        #
        # This is what makes the style factors genuine long-short spreads rather than
        # things the market portfolio is loaded on. Without it the cap-weighted index
        # inherits a large negative size exposure - big companies are, definitionally,
        # not small - and the size factor's volatility lands directly on the benchmark,
        # roughly doubling its volatility. A cap-weighted index should have market
        # exposure and nothing else; the styles should net to zero across it.
        cap_weights = market_cap / market_cap.sum()
        for name in STYLE_FACTORS:
            exposures[name] = exposures[name] - float(exposures[name] @ cap_weights)

        beta = np.clip(rng.normal(1.0, 0.30, size=n), 0.25, 2.2)
        # Low-vol names really are lower vol: tie idiosyncratic risk to the exposure.
        idio = np.clip(
            self.config.idio_vol_mean
            - 0.06 * exposures["lowvol"]
            + rng.normal(0, self.config.idio_vol_disp, size=n),
            0.08,
            0.90,
        )

        sec_ids = [f"SEC{i:05d}" for i in range(n)]
        issuer_ids = [f"ISS{i:05d}" for i in range(n)]

        # ~4% of issuers carry a second share class. Same issuer, different security:
        # the case that breaks ticker-keyed data.
        dual_class = rng.random(n) < 0.04

        listing_start = np.full(n, self.config.start, dtype=object)
        late = rng.random(n) < self.config.frac_late_listings
        span_days = (self.config.end - self.config.start).days
        for i in np.flatnonzero(late):
            offset = int(rng.integers(200, max(400, span_days - 400)))
            listing_start[i] = self.config.start + dt.timedelta(days=offset)

        # Delisting hazard, applied per year of listed life.
        listing_end: list[dt.date | None] = [None] * n
        years = span_days / 365.25
        p_survive = math.exp(-self.config.p_delist_per_year * years)
        delisted = rng.random(n) > p_survive
        for i in np.flatnonzero(delisted):
            earliest = listing_start[i] + dt.timedelta(days=400)
            if earliest >= self.config.end - dt.timedelta(days=60):
                continue
            usable = (self.config.end - dt.timedelta(days=60) - earliest).days
            listing_end[i] = earliest + dt.timedelta(days=int(rng.integers(0, max(1, usable))))

        df = pd.DataFrame(
            {
                "security_id": sec_ids,
                "issuer_id": issuer_ids,
                "listing_id": [f"{s}.{MARKETS[countries[c]][2]}" for s, c in zip(sec_ids,
                                                                                 country_idx,
                                                                                     strict=False)],
                "country": [countries[c].value for c in country_idx],
                "currency": [MARKETS[countries[c]][0].value for c in country_idx],
                "market_status": [MARKETS[countries[c]][1].value for c in country_idx],
                "mic": [MARKETS[countries[c]][2] for c in country_idx],
                "div_frequency": [MARKETS[countries[c]][4] for c in country_idx],
                "icb_industry": [industries[j].value for j in industry_idx],
                "security_type": SecurityType.ORDINARY.value,
                "initial_market_cap": market_cap,
                "beta": beta,
                "idio_vol": idio,
                "listing_start": listing_start,
                "listing_end": listing_end,
                "is_dual_class": dual_class,
            }
        )
        for name in STYLE_FACTORS:
            df[f"expo_{name}"] = exposures[name]

        # Free float: a beta-shaped distribution, deliberately left-skewed for emerging
        # markets where state and founder holdings are larger.
        is_em = df["market_status"] != MarketStatus.DEVELOPED.value
        base_float = rng.beta(6.0, 1.6, size=n)
        em_float = rng.beta(2.6, 2.2, size=n)
        df["initial_float"] = np.where(is_em, em_float, base_float).round(4)

        # Foreign ownership limits exist only in some emerging markets.
        fol = np.ones(n)
        fol_applies = is_em.to_numpy() & (rng.random(n) < 0.30)
        fol[fol_applies] = rng.choice([0.25, 0.30, 0.40, 0.49], size=int(fol_applies.sum()))
        df["foreign_ownership_limit"] = fol

        return df

    # ---------------------------------------------------------------- factor returns

    @cached_property
    def factor_returns(self) -> pd.DataFrame:
        """Daily returns of the latent market, style and industry factors."""
        rng = np.random.default_rng(self.config.seed + 2)
        t = self.n_days
        sqrt_dt = 1.0 / math.sqrt(252.0)

        # Market with a slow volatility regime, so the risk model has something to
        # forecast and the bias test is not trivially satisfied.
        regime = np.zeros(t)
        vol_state = 1.0
        for i in range(t):
            if rng.random() < 0.004:
                vol_state = float(rng.choice([0.80, 1.0, 1.0, 1.35, 1.9]))
            vol_state = 0.995 * vol_state + 0.005 * 1.0
            regime[i] = vol_state
        # The market factor carries the SHOCK only; the drift is applied uniformly in
        # `_returns_matrix` rather than scaled by beta.
        #
        # That flattens the security market line, which is deliberate. Scale the drift
        # by beta and high-beta names mechanically out-earn low-beta ones, so sorting on
        # low volatility sorts on low beta and the low-volatility factor comes out with
        # the wrong sign. The empirical SML *is* flat - that flatness is the entire
        # low-volatility anomaly - so a universe that prices beta linearly cannot
        # contain the effect it claims to.
        mkt = self.config.market_vol * sqrt_dt * regime * rng.standard_normal(t)

        out = {"market": mkt}
        for name, (mu, sigma) in STYLE_PARAMS.items():
            shocks = rng.standard_normal(t)
            # Mild negative autocorrelation: style factors mean-revert at short horizons.
            shocks[1:] -= 0.05 * shocks[:-1]
            out[name] = mu / 252.0 + sigma * sqrt_dt * regime * shocks

        for ind in IcbIndustry:
            out[f"ind_{ind.value}"] = self.config.industry_vol * sqrt_dt * rng.standard_normal(t)

        return pd.DataFrame(out, index=self.calendar)

    # ---------------------------------------------------------------- prices

    @cached_property
    def _returns_matrix(self) -> pd.DataFrame:
        """Security x date total returns, before corporate action price effects."""
        rng = np.random.default_rng(self.config.seed + 3)
        secs = self._security_frame
        f = self.factor_returns
        n, t = len(secs), self.n_days

        r = np.outer(secs["beta"].to_numpy(), f["market"].to_numpy())
        r += self.config.market_drift / 252.0  # uniform, not beta-scaled: flat SML
        for name in STYLE_FACTORS:
            r += np.outer(secs[f"expo_{name}"].to_numpy(), f[name].to_numpy())

        ind_codes = secs["icb_industry"].to_numpy()
        for ind in IcbIndustry:
            mask = ind_codes == ind.value
            if mask.any():
                r[mask] += f[f"ind_{ind.value}"].to_numpy()

        idio_vol = secs["idio_vol"].to_numpy()
        idio = idio_vol[:, None] / math.sqrt(252.0)
        # Student-t innovations: real equity returns are fat-tailed, and a Gaussian
        # universe would make every outlier check in the quality layer look effective.
        shocks = rng.standard_t(df=5, size=(n, t)) / math.sqrt(5 / 3)
        r += idio * shocks

        # Convexity compensation: SUBTRACT half each security's own variance from its
        # log drift.
        #
        # Returns here are compounded as logs, so a security's expected *simple* return
        # is exp(mu + sigma^2/2) - 1, which rises with volatility for free. Between a
        # 15%-vol and a 40%-vol name that is roughly 0.5*(0.40^2 - 0.15^2) = 7%/yr of
        # pure Jensen's inequality - far larger than any premium in STYLE_PARAMS, and
        # enough on its own to reverse the sign of the measured low-volatility factor.
        #
        # Setting mu = drift - var/2 makes expected simple returns equal across
        # securities, so the only thing separating them is the intended factor premia
        # and a measured IC recovers what STYLE_PARAMS configured rather than an
        # artefact of the compounding convention.
        var_mkt = float(f["market"].var())
        var_ind = float(np.mean([f[f"ind_{i.value}"].var() for i in IcbIndustry]))
        total_var = (
            secs["beta"].to_numpy() ** 2 * var_mkt
            + sum(
                secs[f"expo_{name}"].to_numpy() ** 2 * float(f[name].var())
                for name in STYLE_FACTORS
            )
            + var_ind
            + idio_vol**2 / 252.0
        )
        r -= 0.5 * total_var[:, None]

        return pd.DataFrame(r, index=secs["security_id"].to_numpy(), columns=self.calendar)

    @cached_property
    def _generated(self) -> dict[str, pd.DataFrame]:
        """Build prices, corporate actions and share counts in one pass.

        They are generated together because they are not independent: a split changes
        the price *and* the share count on the same date, and a spin-off creates a new
        security whose price series starts on the parent's ex-date.
        """
        if self._tables:
            return self._tables

        rng = np.random.default_rng(self.config.seed + 4)
        secs = self._security_frame
        rets = self._returns_matrix
        cal = self.calendar
        cal_dates = [d.date() for d in cal]
        n, t = len(secs), self.n_days
        year_frac = 1.0 / 252.0

        price = np.full((n, t), np.nan)
        volume = np.full((n, t), np.nan)
        shares_mat = np.full((n, t), np.nan)
        suspended = np.zeros((n, t), dtype=bool)

        actions: list[dict[str, Any]] = []
        share_events: list[dict[str, Any]] = []
        float_events: list[dict[str, Any]] = []
        spinoffs: list[dict[str, Any]] = []

        start_idx = np.searchsorted(
            np.array(cal_dates), secs["listing_start"].to_numpy()
        )
        end_idx = np.array(
            [
                t if e is None else int(np.searchsorted(np.array(cal_dates), e))
                for e in secs["listing_end"]
            ]
        )

        for i in range(n):
            s0, s1 = int(start_idx[i]), int(min(end_idx[i], t))
            if s1 <= s0 + 5:
                continue

            cap0 = float(secs["initial_market_cap"].iloc[i])
            # Choose an initial price in a plausible band, then back out share count.
            p = float(np.exp(rng.normal(3.6, 0.7)))
            sh = cap0 / p
            flt = float(secs["initial_float"].iloc[i])
            div_freq = int(secs["div_frequency"].iloc[i])
            sec_id = str(secs["security_id"].iloc[i])
            ccy = str(secs["currency"].iloc[i])
            country = str(secs["country"].iloc[i])

            # Dividend policy: a payout yield tied to quality, paid on a fixed cycle.
            ann_yield = float(
                np.clip(0.021 + 0.010 * secs["expo_quality"].iloc[i]
                        - 0.008 * secs["expo_growth"].iloc[i]
                        + rng.normal(0, 0.008), 0.0, 0.085)
            )
            div_months = {1: (5,), 2: (5, 11), 4: (2, 5, 8, 11)}[div_freq]

            adv_base = cap0 * flt * float(np.clip(rng.lognormal(-5.6, 0.8), 1e-4, 0.05))
            susp_until = -1

            for j in range(s0, s1):
                d = cal_dates[j]

                # --- suspension -------------------------------------------------
                if j <= susp_until:
                    suspended[i, j] = True
                    price[i, j] = p
                    volume[i, j] = 0.0
                    shares_mat[i, j] = sh
                    continue
                if rng.random() < self.config.p_suspension_per_year * year_frac:
                    susp_until = j + int(rng.integers(2, 15))

                # --- market move -------------------------------------------------
                # Compounded as a LOG return. Treating the factor model's output as a
                # simple return and compounding it drags every price down by sigma^2/2
                # per year - with idiosyncratic vol near 28% that is about -4% a year
                # per name, and it turned the reference index negative over a decade.
                # Log compounding makes the configured drifts geometric, which is what
                # the parameter names claim they are.
                p *= math.exp(float(rets.iat[i, j]))
                p = max(p, 0.01)

                # --- dividends ---------------------------------------------------
                if d.month in div_months and d.day <= 3 and ann_yield > 0:
                    prev = cal_dates[j - 1] if j else None
                    if prev is None or prev.month != d.month:
                        amt = p * ann_yield / div_freq
                        actions.append({
                            "event_id": f"DIV-{sec_id}-{d.isoformat()}",
                            "security_id": sec_id, "event_type": "CASH_DIVIDEND",
                            "announcement_date": d - dt.timedelta(days=21),
                            "ex_date": d,
                            "pay_date": d + dt.timedelta(days=28),
                            "payload": {"amount": round(amt, 6), "currency": ccy,
                                        "gross_amount": round(amt, 6), "is_special": False},
                        })
                        p -= amt

                if rng.random() < self.config.p_special_div_per_year * year_frac:
                    amt = p * float(rng.uniform(0.01, 0.06))
                    actions.append({
                        "event_id": f"SPC-{sec_id}-{d.isoformat()}",
                        "security_id": sec_id, "event_type": "SPECIAL_DIVIDEND",
                        "announcement_date": d - dt.timedelta(days=14),
                        "ex_date": d, "pay_date": d + dt.timedelta(days=21),
                        "payload": {"amount": round(amt, 6), "currency": ccy,
                                    "gross_amount": round(amt, 6), "is_special": True},
                    })
                    p -= amt

                # --- splits -------------------------------------------------------
                if (
                    p > self.config.split_price_threshold
                    and rng.random() < self.config.p_split_per_year * year_frac
                ):
                    ratio = float(rng.choice([2.0, 3.0, 4.0, 5.0, 10.0]))
                    actions.append({
                        "event_id": f"SPL-{sec_id}-{d.isoformat()}",
                        "security_id": sec_id, "event_type": "SPLIT",
                        "announcement_date": d - dt.timedelta(days=30),
                        "ex_date": d, "pay_date": d,
                        "payload": {"ratio": ratio},
                    })
                    p /= ratio
                    sh *= ratio
                    share_events.append({"security_id": sec_id, "effective_date": d,
                                         "knowledge_date": d, "shares_outstanding": sh,
                                         "reason": "SPLIT"})

                elif (
                    p < self.config.reverse_split_price_threshold
                    and rng.random() < self.config.p_reverse_split_per_year * year_frac
                ):
                    ratio = float(rng.choice([0.1, 0.125, 0.2, 0.25]))
                    actions.append({
                        "event_id": f"RSP-{sec_id}-{d.isoformat()}",
                        "security_id": sec_id, "event_type": "REVERSE_SPLIT",
                        "announcement_date": d - dt.timedelta(days=30),
                        "ex_date": d, "pay_date": d,
                        "payload": {"ratio": ratio},
                    })
                    p /= ratio
                    sh *= ratio
                    share_events.append({"security_id": sec_id, "effective_date": d,
                                         "knowledge_date": d, "shares_outstanding": sh,
                                         "reason": "REVERSE_SPLIT"})

                # --- rights issue ---------------------------------------------------
                if rng.random() < self.config.p_rights_issue_per_year * year_frac:
                    n_new, n_held = 1, int(rng.choice([3, 4, 5, 6]))
                    discount = float(rng.uniform(0.15, 0.40))
                    sub_price = p * (1 - discount)
                    terp = (n_held * p + n_new * sub_price) / (n_held + n_new)
                    actions.append({
                        "event_id": f"RTS-{sec_id}-{d.isoformat()}",
                        "security_id": sec_id, "event_type": "RIGHTS_ISSUE",
                        "announcement_date": d - dt.timedelta(days=25),
                        "ex_date": d, "pay_date": d + dt.timedelta(days=30),
                        "payload": {"new_shares": n_new, "per_held": n_held,
                                    "subscription_price": round(sub_price, 6),
                                    "cum_price": round(p, 6), "terp": round(terp, 6),
                                    "currency": ccy},
                    })
                    p = terp
                    sh *= 1 + n_new / n_held
                    share_events.append({"security_id": sec_id, "effective_date": d,
                                         "knowledge_date": d, "shares_outstanding": sh,
                                         "reason": "RIGHTS_ISSUE"})

                # --- spin-off ---------------------------------------------------------
                if (
                    rng.random() < self.config.p_spinoff_per_year * year_frac
                    and j < s1 - 260
                    and cap0 > self.config.spinoff_min_market_cap
                ):
                    frac = float(rng.uniform(0.12, 0.35))
                    spin_id = f"{sec_id}-SPIN{len(spinoffs):03d}"
                    ratio = float(rng.choice([0.25, 0.5, 1.0]))
                    spin_value = p * frac
                    actions.append({
                        "event_id": f"SPN-{sec_id}-{d.isoformat()}",
                        "security_id": sec_id, "event_type": "SPINOFF",
                        "announcement_date": d - dt.timedelta(days=120),
                        "ex_date": d, "pay_date": d,
                        "payload": {"spinco_security_id": spin_id,
                                    "shares_per_parent_share": ratio,
                                    "value_per_parent_share": round(spin_value, 6),
                                    "parent_cum_price": round(p, 6), "currency": ccy},
                    })
                    spinoffs.append({"parent": sec_id, "spin_id": spin_id, "ex_index": j,
                                     "value_per_share": spin_value, "ratio": ratio,
                                     "parent_shares": sh, "country": country,
                                     "currency": ccy, "parent_row": i})
                    p -= spin_value

                # --- share count drift --------------------------------------------
                if rng.random() < self.config.p_buyback_per_year * year_frac:
                    delta = float(rng.normal(-0.012, 0.018))
                    sh *= 1 + delta
                    share_events.append({
                        "security_id": sec_id, "effective_date": d,
                        # Share counts are known from a filing, which lags the event.
                        "knowledge_date": d + dt.timedelta(days=int(rng.integers(5, 45))),
                        "shares_outstanding": sh,
                        "reason": "BUYBACK" if delta < 0 else "ISSUANCE",
                    })

                if rng.random() < self.config.p_float_change_per_year * year_frac:
                    flt = float(np.clip(flt + rng.normal(0, 0.035), 0.02, 1.0))
                    float_events.append({
                        "security_id": sec_id, "effective_date": d,
                        "knowledge_date": d + dt.timedelta(days=int(rng.integers(2, 20))),
                        "free_float_factor": round(flt, 4),
                    })

                price[i, j] = p
                shares_mat[i, j] = sh
                volume[i, j] = max(
                    0.0, adv_base / p * float(rng.lognormal(0, 0.55))
                )

            # --- terminal event ----------------------------------------------------
            # Three ways a constituent leaves: bought for cash (weight exits as cash),
            # bought for stock (weight transfers to the acquirer, which is the harder
            # case for a replicating fund), or plain delisting.
            end_date = secs["listing_end"].iloc[i]
            if end_date is not None and s1 < t:
                roll = rng.random()
                premium = float(rng.uniform(0.10, 0.45))
                if roll < 0.70 * (1 - self.config.frac_mergers_stock):
                    kind, payload = "MERGER_CASH", {
                        "cash_per_share": round(p * (1 + premium), 6), "currency": ccy,
                    }
                elif roll < 0.70:
                    kind, payload = "MERGER_STOCK", {
                        # Acquirer resolved after generation, once survivors are known.
                        "acquirer_security_id": None,
                        "exchange_ratio": round(float(rng.uniform(0.15, 2.5)), 6),
                        "implied_value_per_share": round(p * (1 + premium), 6),
                        "currency": ccy,
                    }
                else:
                    kind, payload = "DELISTING", {
                        "reason": "DELISTED", "final_price": round(p, 6),
                    }
                actions.append({
                    "event_id": f"END-{sec_id}-{end_date.isoformat()}",
                    "security_id": sec_id, "event_type": kind,
                    "announcement_date": end_date - dt.timedelta(days=75),
                    "ex_date": end_date, "pay_date": end_date,
                    "payload": payload,
                })

        # --- spin-off children get their own price series ------------------------
        spin_rows: list[dict[str, Any]] = []
        for spin_index, spin in enumerate(spinoffs):
            i, j0 = int(spin["parent_row"]), int(spin["ex_index"])
            spin_price = spin["value_per_share"] / spin["ratio"]
            spin_shares = spin["parent_shares"] * spin["ratio"]
            # Seed from the spin-off's position, NOT from hash(spin_id).
            #
            # Python randomises string hashing per process unless PYTHONHASHSEED is
            # fixed, so the previous version produced a different spinco price series
            # on every run. It survived the small-universe determinism test because
            # that window happens to contain no spin-offs, and was caught by the golden
            # master at 300 securities: 5.3bp of drift between two supposedly identical
            # builds.
            #
            # Exactly the class of defect a run manifest exists to surface - identical
            # code, identical inputs, different output.
            srng = np.random.default_rng(self.config.seed + 5 + spin_index * 7919)
            pp = spin_price
            for j in range(j0, t):
                pp *= math.exp(float(rets.iat[i, j]) + float(srng.normal(0, 0.012)))
                pp = max(pp, 0.05)
                spin_rows.append({
                    "security_id": spin["spin_id"], "date": cal_dates[j],
                    "close": pp, "shares": spin_shares,
                    "volume": spin_shares * 0.004 * float(srng.lognormal(0, 0.6)),
                    "currency": spin["currency"], "country": spin["country"],
                })

        self._tables = self._assemble(
            price, volume, shares_mat, suspended, actions, share_events,
            float_events, spin_rows, cal_dates,
        )
        return self._tables

    # ---------------------------------------------------------------- assembly

    def _assemble(
        self,
        price: np.ndarray,
        volume: np.ndarray,
        shares_mat: np.ndarray,
        suspended: np.ndarray,
        actions: list[dict[str, Any]],
        share_events: list[dict[str, Any]],
        float_events: list[dict[str, Any]],
        spin_rows: list[dict[str, Any]],
        cal_dates: list[dt.date],
    ) -> dict[str, pd.DataFrame]:
        secs = self._security_frame
        rng = np.random.default_rng(self.config.seed + 6)

        # ---- prices -------------------------------------------------------------
        mask = ~np.isnan(price)
        sec_ix, date_ix = np.nonzero(mask)
        closes = price[mask]
        prices = pd.DataFrame({
            "security_id": secs["security_id"].to_numpy()[sec_ix],
            "listing_id": secs["listing_id"].to_numpy()[sec_ix],
            "date": np.array(cal_dates, dtype=object)[date_ix],
            "close": closes,
            "volume": volume[mask],
            "currency": secs["currency"].to_numpy()[sec_ix],
            "is_suspended": suspended[mask],
        })
        # Intraday range around the close. Not used by the index (which is a close-based
        # calculation) but the quality layer's range checks need it.
        # High and low must bracket both open and close by construction. Scaling
        # max(open, close) by a lognormal whose draw can fall below 1.0 lets the high
        # print under the close, which is not a market that exists - and the quality
        # layer's OHLC check correctly flags it as corrupt data.
        noise = rng.lognormal(0, 0.006, size=len(prices))
        prices["open"] = prices["close"] / noise
        upper = np.maximum(prices["open"], prices["close"])
        lower = np.minimum(prices["open"], prices["close"])
        prices["high"] = upper * (1.0 + np.abs(rng.normal(0, 0.004, size=len(prices))))
        prices["low"] = lower / (1.0 + np.abs(rng.normal(0, 0.004, size=len(prices))))

        if spin_rows:
            spin_df = pd.DataFrame(spin_rows)
            spin_df["listing_id"] = spin_df["security_id"] + ".XNYS"
            spin_df["is_suspended"] = False
            spin_df["open"] = spin_df["close"]
            spin_df["high"] = spin_df["close"] * 1.004
            spin_df["low"] = spin_df["close"] * 0.996
            prices = pd.concat(
                [prices, spin_df[["security_id", "listing_id", "date", "close", "volume",
                                  "currency", "is_suspended", "open", "high", "low"]]],
                ignore_index=True,
            )

        prices = prices.sort_values(["security_id", "date"]).reset_index(drop=True)

        # ---- shares and float ----------------------------------------------------
        initial_shares = []
        for i, sid in enumerate(secs["security_id"]):
            row = np.flatnonzero(~np.isnan(shares_mat[i]))
            if row.size:
                initial_shares.append({
                    "security_id": sid,
                    "effective_date": cal_dates[int(row[0])],
                    "knowledge_date": cal_dates[int(row[0])],
                    "shares_outstanding": float(shares_mat[i, row[0]]),
                    "reason": "INITIAL",
                })
        shares_df = pd.DataFrame(initial_shares + share_events)

        float_initial = pd.DataFrame({
            "security_id": secs["security_id"],
            "effective_date": secs["listing_start"],
            "knowledge_date": secs["listing_start"],
            "free_float_factor": secs["initial_float"],
        })
        float_df = pd.concat([float_initial, pd.DataFrame(float_events)], ignore_index=True)

        # Share-count events and free-float events arrive independently, each carrying
        # only its own field. Interleave them on the event timeline and forward-fill
        # within each security, so every row is a complete picture of what was known.
        fol = dict(zip(secs["security_id"], secs["foreign_ownership_limit"], strict=False))

        shares_part = shares_df.copy()
        shares_part["free_float_factor"] = np.nan

        float_part = float_df.copy()
        float_part["shares_outstanding"] = np.nan
        float_part["reason"] = "FLOAT_CHANGE"

        cols = ["security_id", "effective_date", "knowledge_date",
                "shares_outstanding", "free_float_factor", "reason"]
        shares_final = (
            pd.concat([shares_part[cols], float_part[cols]], ignore_index=True)
            .sort_values(["security_id", "effective_date", "knowledge_date"],
                         kind="mergesort")
            .reset_index(drop=True)
        )
        grouped = shares_final.groupby("security_id", sort=False)
        shares_final["shares_outstanding"] = grouped["shares_outstanding"].ffill()
        shares_final["free_float_factor"] = grouped["free_float_factor"].ffill()

        # A float observation before the first share count has nothing to attach to.
        shares_final = shares_final.dropna(subset=["shares_outstanding"]).copy()
        shares_final["free_float_factor"] = shares_final["free_float_factor"].fillna(1.0)
        shares_final["foreign_ownership_limit"] = (
            shares_final["security_id"].map(fol).fillna(1.0)
        )
        shares_final = shares_final.reset_index(drop=True)

        # ---- corporate actions ----------------------------------------------------
        # Stock mergers were emitted without an acquirer, because the acquirer must
        # itself still be listed on the ex-date and that is only knowable once every
        # security's terminal event has been drawn.
        alive_from = dict(zip(secs["security_id"], secs["listing_start"], strict=False))
        alive_to = dict(zip(secs["security_id"], secs["listing_end"], strict=False))
        for action in actions:
            if action["event_type"] != "MERGER_STOCK":
                continue
            ex = action["ex_date"]
            candidates = [
                s for s, start in alive_from.items()
                if start <= ex and (alive_to[s] is None or alive_to[s] > ex)
                and s != action["security_id"]
            ]
            if candidates:
                action["payload"]["acquirer_security_id"] = candidates[
                    int(rng.integers(0, len(candidates)))
                ]
            else:
                # No surviving acquirer: degrade to a cash deal rather than emit an
                # event the engine cannot apply.
                action["event_type"] = "MERGER_CASH"
                action["payload"] = {
                    "cash_per_share": action["payload"]["implied_value_per_share"],
                    "currency": action["payload"]["currency"],
                }

        ca = pd.DataFrame(actions)
        if not ca.empty:
            ca["payload"] = ca["payload"].apply(json.dumps)
            ca = ca.sort_values(["ex_date", "security_id"]).reset_index(drop=True)

        return {
            "prices": prices,
            "shares": shares_final,
            "corp_actions": ca,
            "securities": secs,
        }

    # ---------------------------------------------------------------- fundamentals

    @cached_property
    def _fundamentals(self) -> pd.DataFrame:
        """Quarterly fundamentals with filing lags and occasional restatements.

        Values are generated from the latent style exposures, so book-to-price recovers
        a noisy view of the true value exposure. The restatements are the point of the
        exercise: the same `period_end` appears twice with different `filed_date` and
        different `value`, and a query that ignores `filed_date` will silently use the
        later number.
        """
        rng = np.random.default_rng(self.config.seed + 7)
        secs = self._security_frame
        rows: list[dict[str, Any]] = []

        periods = pd.date_range(
            self.config.start - dt.timedelta(days=400), self.config.end, freq="QE"
        )

        for _, sec in secs.iterrows():
            sid = str(sec["security_id"])
            cap = float(sec["initial_market_cap"])
            ccy = str(sec["currency"])

            # Book-to-price implied by the latent value exposure, then inverted to a
            # book equity level. Quality drives margin; growth drives revenue trend.
            btp = float(np.clip(0.55 * math.exp(0.45 * sec["expo_value"]), 0.03, 4.0))
            book = cap * btp
            margin = float(np.clip(0.085 + 0.035 * sec["expo_quality"]
                                   + rng.normal(0, 0.02), -0.10, 0.42))
            asset_turn = float(np.clip(0.70 + 0.20 * rng.standard_normal(), 0.15, 2.5))
            growth = float(np.clip(0.03 + 0.055 * sec["expo_growth"], -0.12, 0.35))

            revenue = cap * 0.62 * float(rng.lognormal(0, 0.35))
            assets = max(revenue / asset_turn, book * 1.05)
            debt = assets * float(np.clip(rng.beta(2.2, 4.0), 0.0, 0.75))

            for k, period_end in enumerate(periods):
                pe = period_end.date()
                q_growth = (1 + growth) ** 0.25
                revenue *= q_growth * float(rng.lognormal(0, 0.045))
                assets *= q_growth * float(rng.lognormal(0, 0.030))
                debt *= float(rng.lognormal(0, 0.05))
                ni = revenue * margin * float(rng.lognormal(0, 0.22))
                book = max(book * 1.004 + ni * 0.62, assets * 0.03)
                gp = revenue * float(np.clip(margin * 3.1 + rng.normal(0, 0.04), 0.02, 0.75))

                lag = int(rng.integers(*self.config.fundamental_lag_days))
                filed = pe + dt.timedelta(days=lag)
                if filed > self.config.end:
                    continue

                values = {
                    "BOOK_EQUITY": book,
                    "NET_INCOME": ni,
                    "REVENUE": revenue,
                    "TOTAL_ASSETS": assets,
                    "TOTAL_DEBT": debt,
                    "GROSS_PROFIT": gp,
                    "OPERATING_CASHFLOW": ni * float(rng.uniform(0.85, 1.55)),
                    "CAPEX": -assets * float(rng.uniform(0.008, 0.045)),
                    "DIVIDENDS_PAID": -max(0.0, ni * float(rng.uniform(0.0, 0.6))),
                }
                for item, val in values.items():
                    rows.append({"security_id": sid, "item": item, "period_end": pe,
                                 "filed_date": filed, "value": float(val), "currency": ccy,
                                 "is_restatement": False})

                # Restatement: same period, filed later, different number.
                if k > 2 and rng.random() < self.config.p_restatement:
                    rlag = int(rng.integers(*self.config.restatement_lag_days))
                    refiled = pe + dt.timedelta(days=rlag)
                    if refiled <= self.config.end:
                        shift = float(rng.normal(0, 0.07))
                        for item in ("BOOK_EQUITY", "NET_INCOME", "TOTAL_ASSETS"):
                            rows.append({
                                "security_id": sid, "item": item, "period_end": pe,
                                "filed_date": refiled,
                                "value": float(values[item] * (1 + shift)),
                                "currency": ccy, "is_restatement": True,
                            })

        return pd.DataFrame(rows).sort_values(
            ["security_id", "item", "period_end", "filed_date"]).reset_index(drop=True)

    # ---------------------------------------------------------------- FX

    @cached_property
    def _fx(self) -> pd.DataFrame:
        """Spot rates against the base currency, plus deposit rates for CIP forwards."""
        rng = np.random.default_rng(self.config.seed + 8)
        base = self.config.base_currency.value
        rows: list[dict[str, Any]] = []
        sqrt_dt = 1.0 / math.sqrt(252.0)

        levels = {Currency.GBP: 1.30, Currency.EUR: 1.10, Currency.JPY: 0.0080,
                  Currency.CHF: 1.05, Currency.CAD: 0.76, Currency.AUD: 0.70,
                  Currency.HKD: 0.128, Currency.SEK: 0.105, Currency.KRW: 0.00082,
                  Currency.USD: 1.0}
        rates = {Currency.USD: 0.030, Currency.GBP: 0.032, Currency.EUR: 0.018,
                 Currency.JPY: 0.002, Currency.CHF: 0.008, Currency.CAD: 0.028,
                 Currency.AUD: 0.031, Currency.HKD: 0.029, Currency.SEK: 0.020,
                 Currency.KRW: 0.027}

        for ccy, lvl in levels.items():
            vol = 0.0 if ccy == Currency.USD else float(rng.uniform(0.05, 0.13))
            r = rates[ccy]
            x = lvl
            for j, d in enumerate(self.calendar):
                if ccy != Currency.USD:
                    x *= math.exp(-0.5 * vol**2 / 252 + vol * sqrt_dt
                                  * float(rng.standard_normal()))
                    r = float(np.clip(r + rng.normal(0, 0.00012), 0.0, 0.10))
                rows.append({"date": d.date(), "base": base, "quote": ccy.value,
                             "rate": x, "deposit_rate": r})
                del j
        return pd.DataFrame(rows)

    # ---------------------------------------------------------------- table accessors

    # The pipeline needs the whole table, not an as-of slice: `production.build` hands
    # the full price panel to `IndexCalculator.run`, which walks it day by day. These
    # five properties are that access path. They exist so nothing outside this module
    # has to name `_generated`, which was the coupling that made the provider Protocol
    # in `data.providers` decorative - a real universe could satisfy every documented
    # method and still not be substitutable here.

    @property
    def prices(self) -> pd.DataFrame:
        return self._generated["prices"]

    @property
    def shares(self) -> pd.DataFrame:
        return self._generated["shares"]

    @property
    def corp_actions(self) -> pd.DataFrame:
        return self._generated["corp_actions"]

    @property
    def fundamentals(self) -> pd.DataFrame:
        return self._fundamentals

    @property
    def fx_rates(self) -> pd.DataFrame:
        return self._fx

    # ------------------------------------------------------------------ identity

    @property
    def fingerprint(self) -> str:
        """Identity of the data, for the run manifest.

        For a generator this is a hash of the configuration, because the config plus
        the seed fully determines the tables. A materialised universe hashes the files
        instead. Both answer the same question - "which data was this built from" - and
        that is what the manifest records.
        """
        return self.config.fingerprint()

    @property
    def start(self) -> dt.date:
        return self.config.start

    @property
    def end(self) -> dt.date:
        return self.config.end

    # ---------------------------------------------------------------- provider API

    def get_prices(self, listing_ids: list[str] | None, start: dt.date, end: dt.date
                   ) -> pd.DataFrame:
        df = self._generated["prices"]
        out = df[(df["date"] >= start) & (df["date"] <= end)]
        if listing_ids is not None:
            out = out[out["listing_id"].isin(listing_ids)]
        return out.reset_index(drop=True)

    def get_shares(self, security_ids: list[str] | None, as_of: dt.date) -> pd.DataFrame:
        df = self._generated["shares"]
        known = df[df["knowledge_date"] <= as_of]
        if security_ids is not None:
            known = known[known["security_id"].isin(security_ids)]
        return (
            known.sort_values(["security_id", "effective_date", "knowledge_date"])
            .groupby("security_id", as_index=False)
            .last()
            .reset_index(drop=True)
        )

    def get_shares_history(self, security_ids: list[str] | None, start: dt.date,
                           end: dt.date) -> pd.DataFrame:
        df = self._generated["shares"]
        out = df[(df["effective_date"] >= start) & (df["effective_date"] <= end)]
        if security_ids is not None:
            out = out[out["security_id"].isin(security_ids)]
        return out.reset_index(drop=True)

    def get_fundamentals(self, security_ids: list[str] | None, items: list[str],
                         as_of: dt.date, max_staleness_days: int = 550) -> pd.DataFrame:
        df = self._fundamentals
        # The contract: nothing filed after as_of, ever.
        known = df[(df["filed_date"] <= as_of) & (df["item"].isin(items))]
        cutoff = as_of - dt.timedelta(days=max_staleness_days)
        known = known[known["period_end"] >= cutoff]
        if security_ids is not None:
            known = known[known["security_id"].isin(security_ids)]
        return (
            known.sort_values(["security_id", "item", "period_end", "filed_date"])
            .groupby(["security_id", "item"], as_index=False)
            .last()
            .reset_index(drop=True)
        )

    def get_fundamentals_ttm(self, security_ids: list[str] | None, item: str,
                             as_of: dt.date) -> pd.DataFrame:
        """Trailing four quarters of a flow item, using only filings known on `as_of`."""
        df = self._fundamentals
        known = df[(df["filed_date"] <= as_of) & (df["item"] == item)]
        if security_ids is not None:
            known = known[known["security_id"].isin(security_ids)]
        latest = (
            known.sort_values(["security_id", "period_end", "filed_date"])
            .groupby(["security_id", "period_end"], as_index=False)
            .last()
        )
        latest = latest.sort_values(["security_id", "period_end"])
        top4 = latest.groupby("security_id").tail(4)
        agg = top4.groupby("security_id", as_index=False).agg(
            value=("value", "sum"), n_periods=("value", "size"),
            latest_period=("period_end", "max"),
        )
        return agg[agg["n_periods"] == 4].reset_index(drop=True)

    def get_fundamentals_raw(self) -> pd.DataFrame:
        """The whole bitemporal fundamentals table, unfiltered.

        For callers that will apply their own `filed_date` bound and want to do it once
        rather than per query. Named `_raw` as a warning: using it without a PIT filter
        is a look-ahead bug, and the name is there so that shows up in review.
        """
        return self._fundamentals

    def get_corp_actions(self, security_ids: list[str] | None, start: dt.date,
                         end: dt.date) -> pd.DataFrame:
        df = self._generated["corp_actions"]
        if df.empty:
            return df
        out = df[(df["ex_date"] >= start) & (df["ex_date"] <= end)]
        if security_ids is not None:
            out = out[out["security_id"].isin(security_ids)]
        return out.reset_index(drop=True)

    def get_fx(self, base: str, quotes: list[str], start: dt.date, end: dt.date
               ) -> pd.DataFrame:
        df = self._fx
        out = df[(df["date"] >= start) & (df["date"] <= end) & (df["quote"].isin(quotes))]
        return out[["date", "base", "quote", "rate"]].reset_index(drop=True)

    def get_deposit_rates(self, currencies: list[str], start: dt.date, end: dt.date
                          ) -> pd.DataFrame:
        df = self._fx
        out = df[(df["date"] >= start) & (df["date"] <= end) & (df["quote"].isin(currencies))]
        return out[["date", "quote", "deposit_rate"]].rename(
            columns={"quote": "currency"}).reset_index(drop=True)

    def get_classifications(self, security_ids: list[str] | None, as_of: dt.date
                            ) -> pd.DataFrame:
        secs = self._security_frame
        out = secs[["security_id", "icb_industry"]].copy()
        out["effective_date"] = self.config.start
        out["knowledge_date"] = self.config.start
        out["icb_supersector"] = out["icb_industry"] + "10"
        if security_ids is not None:
            out = out[out["security_id"].isin(security_ids)]
        del as_of
        return out.reset_index(drop=True)

    def get_issuers(self) -> pd.DataFrame:
        secs = self._security_frame
        return secs[["issuer_id", "country", "market_status"]].drop_duplicates(
            "issuer_id").reset_index(drop=True)

    def get_securities(self) -> pd.DataFrame:
        cols = ["security_id", "issuer_id", "country", "currency", "market_status",
                "icb_industry", "security_type", "listing_start", "listing_end",
                "is_dual_class", "foreign_ownership_limit"]
        return self._security_frame[cols].copy()

    def get_listings(self) -> pd.DataFrame:
        cols = ["listing_id", "security_id", "mic", "currency", "country",
                "listing_start", "listing_end"]
        return self._security_frame[cols].copy()

    def get_identifier_map(self) -> pd.DataFrame:
        """Synthetic ISIN/SEDOL/ticker with correct check digits.

        Generated rather than random so `secmaster.identifiers` validation passes on the
        reference universe - a validator that only ever sees valid input is untested,
        so the test suite supplies the invalid cases separately.
        """
        from miniftse.secmaster.identifiers import make_isin, make_sedol

        secs = self._security_frame
        rows: list[dict[str, Any]] = []
        for i, (sid, country, lid) in enumerate(
            zip(secs["security_id"], secs["country"], secs["listing_id"], strict=False)
        ):
            rows.append({
                "security_id": sid, "listing_id": lid,
                "isin": make_isin(str(country), i),
                "sedol": make_sedol(i),
                "ticker": f"SY{i:04d}",
                "valid_from": self.config.start, "valid_to": None,
            })
        return pd.DataFrame(rows)

    # ---------------------------------------------------------------- caching

    def materialise(self, path: Path) -> dict[str, Path]:
        """Write every table to parquet. Used by the CLI to make runs fast and by CI
        to keep the golden master honest."""
        path.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        tables = {
            "prices": self._generated["prices"],
            "shares": self._generated["shares"],
            "corp_actions": self._generated["corp_actions"],
            "securities": self.get_securities(),
            "listings": self.get_listings(),
            "identifiers": self.get_identifier_map(),
            "fundamentals": self._fundamentals,
            "fx": self._fx,
            "factor_returns": self.factor_returns.reset_index(names="date"),
        }
        for name, df in tables.items():
            dest = path / f"{name}.parquet"
            out = df.copy()
            for col in out.columns:
                if out[col].dtype == object and len(out) and isinstance(
                    out[col].dropna().iloc[0] if out[col].notna().any() else None, dt.date
                ):
                    out[col] = pd.to_datetime(out[col])
            out.to_parquet(dest, index=False)
            written[name] = dest
        (path / "config.json").write_text(
            json.dumps({"fingerprint": self.config.fingerprint(),
                        "seed": self.config.seed,
                        "n_securities": self.config.n_securities,
                        "start": self.config.start.isoformat(),
                        "end": self.config.end.isoformat()}, indent=2)
        )
        return written

    def summary(self) -> dict[str, Any]:
        g = self._generated
        ca = g["corp_actions"]
        return {
            "fingerprint": self.config.fingerprint(),
            "securities": int(len(self._security_frame)),
            "trading_days": self.n_days,
            "price_rows": int(len(g["prices"])),
            "delisted": int(self._security_frame["listing_end"].notna().sum()),
            "late_listings": int(
                (self._security_frame["listing_start"] > self.config.start).sum()),
            "corp_actions": (
                ca["event_type"].value_counts().to_dict() if not ca.empty else {}
            ),
            "fundamental_rows": int(len(self._fundamentals)),
            "restatements": int(self._fundamentals["is_restatement"].sum()),
        }
