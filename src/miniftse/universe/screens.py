"""Eligibility screening.

A screen answers "may this security be in the index at all", before any question of
which size band or what weight. The rules themselves are simple; two things about how
they are applied are not, and both are what separates an index from a stock filter:

* **Every rejection is recorded with the value that caused it.** "Why is Company X not
  in your index?" is a routine client enquiry and the answer has to be a number, not a
  shrug. `ScreenReport` keeps the failing value for every failing rule.
* **Incumbents and candidates are screened differently.** A name already in the index
  gets a looser test than one trying to join. Without that asymmetry, a security
  hovering at a threshold enters and leaves at alternate reviews, generating turnover
  that costs tracking funds real money and tells investors nothing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from miniftse.config import EligibilityConfig
from miniftse.types import MarketStatus, SecurityType


@dataclass(frozen=True, slots=True)
class ScreenOutcome:
    """One rule's verdict on one security."""

    rule: str
    passed: bool
    value: float | str | None
    threshold: float | str | None
    detail: str = ""

    def explain(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return f"[{verdict}] {self.rule}: {self.value} vs {self.threshold}. {self.detail}".strip()


@dataclass
class ScreenReport:
    """All outcomes for one security, plus the overall verdict."""

    security_id: str
    outcomes: list[ScreenOutcome] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return all(o.passed for o in self.outcomes)

    @property
    def failures(self) -> list[ScreenOutcome]:
        return [o for o in self.outcomes if not o.passed]

    def explain(self) -> str:
        head = f"{self.security_id}: {'ELIGIBLE' if self.eligible else 'INELIGIBLE'}"
        return head + "\n" + "\n".join("  " + o.explain() for o in self.outcomes)

    def client_answer(self) -> str:
        """The one-line reason to put in a client email."""
        if self.eligible:
            return f"{self.security_id} meets all eligibility criteria."
        first = self.failures[0]
        return (
            f"{self.security_id} is excluded because {first.rule} is {first.value} "
            f"against a requirement of {first.threshold}."
        )


@dataclass(frozen=True, slots=True)
class SecurityMetrics:
    """The computed inputs a screen needs, all as at the review cut-off date."""

    security_id: str
    free_float_factor: float
    foreign_ownership_limit: float
    float_market_cap: float
    median_daily_turnover_ratio: float
    """Median daily traded value divided by free-float market cap, over the test
    window. A ratio rather than a level, so the threshold means the same thing for a
    mega-cap and a small-cap."""

    price_observations: int
    security_type: SecurityType
    market_status: MarketStatus
    listing_age_days: int
    is_suspended: bool = False
    has_local_line_in_index: bool = False
    """True for a depositary receipt whose underlying local line is already a
    constituent. Holding both double-counts the issuer."""

    @property
    def investable_factor(self) -> float:
        return min(self.free_float_factor, self.foreign_ownership_limit)


ScreenRule = Callable[[SecurityMetrics, EligibilityConfig, bool], ScreenOutcome]
"""(metrics, config, is_incumbent) -> outcome."""


# --------------------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------------------

INCUMBENT_RELIEF = 0.75
"""Incumbents are tested at 75% of the entry threshold. A name must fall a quarter
below the bar before it is removed, which is what stops threshold-hovering from
generating turnover. The number is a design choice; the study behind it is in the
buffer analysis in `research/`."""


def screen_free_float(
    m: SecurityMetrics, cfg: EligibilityConfig, incumbent: bool
) -> ScreenOutcome:
    """Minimum investable proportion, with a higher bar in emerging markets.

    Emerging markets carry more state and founder ownership, and a thin float in a
    shallow market is not investable at index scale even when the percentage looks fine.
    """
    threshold = (
        cfg.min_free_float_developed
        if m.market_status == MarketStatus.DEVELOPED
        else cfg.min_free_float_emerging
    )
    if incumbent:
        threshold *= INCUMBENT_RELIEF
    return ScreenOutcome(
        rule="free_float",
        passed=m.investable_factor >= threshold,
        value=round(m.investable_factor, 4),
        threshold=round(threshold, 4),
        detail=(
            f"free float {m.free_float_factor:.2%} capped by a "
            f"{m.foreign_ownership_limit:.0%} foreign ownership limit"
            if m.foreign_ownership_limit < m.free_float_factor
            else f"{m.market_status} threshold"
        ),
    )


def screen_liquidity(
    m: SecurityMetrics, cfg: EligibilityConfig, incumbent: bool
) -> ScreenOutcome:
    """Median daily turnover as a fraction of free-float market cap.

    Median rather than mean: one takeover-rumour day should not qualify a security that
    is untradeable the rest of the year. That single word is the difference between a
    screen that works and one that is gamed.
    """
    threshold = cfg.min_liquidity_turnover * (INCUMBENT_RELIEF if incumbent else 1.0)
    return ScreenOutcome(
        rule="liquidity",
        passed=m.median_daily_turnover_ratio >= threshold,
        value=round(m.median_daily_turnover_ratio, 6),
        threshold=round(threshold, 6),
        detail=f"median daily turnover over {cfg.liquidity_window_days} sessions",
    )


def screen_price_history(
    m: SecurityMetrics, cfg: EligibilityConfig, incumbent: bool
) -> ScreenOutcome:
    """Enough trading days in the window to have measured anything.

    Catches long suspensions and recent listings. New listings large enough to matter
    come in through fast entry instead, which is a deliberate exception rather than a
    hole in this rule.
    """
    threshold = cfg.min_price_observations * (0.5 if incumbent else 1.0)
    return ScreenOutcome(
        rule="price_history",
        passed=m.price_observations >= threshold,
        value=m.price_observations,
        threshold=int(threshold),
        detail="trading days with a price in the test window",
    )


def screen_size(m: SecurityMetrics, cfg: EligibilityConfig, incumbent: bool) -> ScreenOutcome:
    threshold = cfg.min_free_float_market_cap * (INCUMBENT_RELIEF if incumbent else 1.0)
    return ScreenOutcome(
        rule="minimum_size",
        passed=m.float_market_cap >= threshold,
        value=round(m.float_market_cap, 0),
        threshold=round(threshold, 0),
        detail="free-float market cap in index base currency",
    )


def screen_security_type(
    m: SecurityMetrics, cfg: EligibilityConfig, incumbent: bool
) -> ScreenOutcome:
    """Instrument form.

    Depositary receipts get a conditional rule rather than a flat exclusion: excluded
    where the local line is itself in the index, permitted where it is the only
    accessible route to the issuer. That conditionality is real methodology, and it is
    why the rule cannot be a simple set membership test.
    """
    del incumbent
    if m.security_type in (SecurityType.ADR, SecurityType.GDR):
        return ScreenOutcome(
            rule="security_type",
            passed=not m.has_local_line_in_index,
            value=str(m.security_type),
            threshold="excluded where the local line is a constituent",
            detail=(
                "local line already held, so the receipt would double-count the issuer"
                if m.has_local_line_in_index
                else "no local line in the index, so the receipt is the accessible line"
            ),
        )
    excluded = m.security_type in cfg.excluded_security_types
    if m.security_type == SecurityType.PREFERRED:
        excluded = not cfg.allow_preferred
    return ScreenOutcome(
        rule="security_type",
        passed=not excluded,
        value=str(m.security_type),
        threshold="ordinary lines and REITs",
        detail="",
    )


def screen_suspension(
    m: SecurityMetrics, cfg: EligibilityConfig, incumbent: bool
) -> ScreenOutcome:
    """A suspended security cannot join, but is not deleted on suspension alone.

    Deleting immediately would crystallise a price nobody can trade at. The standard
    treatment is to hold at the last price, review the position, and remove only when
    the suspension becomes permanent - which is the answer to the "a stock in your index
    was suspended three weeks ago" client question.
    """
    del cfg
    return ScreenOutcome(
        rule="suspension",
        passed=incumbent or not m.is_suspended,
        value="suspended" if m.is_suspended else "trading",
        threshold="trading at the cut-off",
        detail=(
            "incumbents are retained through a suspension and valued at the last "
            "traded price" if incumbent and m.is_suspended else ""
        ),
    )


DEFAULT_RULES: tuple[ScreenRule, ...] = (
    screen_security_type,
    screen_free_float,
    screen_size,
    screen_liquidity,
    screen_price_history,
    screen_suspension,
)


# --------------------------------------------------------------------------------------


@dataclass
class EligibilityScreener:
    """Runs the rule set over a universe and reports the outcome for every security."""

    config: EligibilityConfig
    rules: tuple[ScreenRule, ...] = DEFAULT_RULES

    def screen_one(self, m: SecurityMetrics, incumbent: bool = False) -> ScreenReport:
        return ScreenReport(
            security_id=m.security_id,
            outcomes=[rule(m, self.config, incumbent) for rule in self.rules],
        )

    def screen_all(
        self, metrics: list[SecurityMetrics], incumbents: set[str] | None = None
    ) -> dict[str, ScreenReport]:
        incumbents = incumbents or set()
        return {
            m.security_id: self.screen_one(m, m.security_id in incumbents)
            for m in metrics
        }

    def eligible_ids(
        self, metrics: list[SecurityMetrics], incumbents: set[str] | None = None
    ) -> list[str]:
        return [
            sid for sid, rep in self.screen_all(metrics, incumbents).items() if rep.eligible
        ]

    @staticmethod
    def rejection_summary(reports: dict[str, ScreenReport]) -> pd.DataFrame:
        """Counts by failing rule.

        Read this after every review. A sudden jump in one rule's rejections is almost
        always a data problem rather than a market event - a float file that failed to
        load, an FX rate inverted, a volume field arriving in the wrong units.
        """
        rows = [
            {"rule": o.rule, "security_id": sid}
            for sid, rep in reports.items()
            for o in rep.failures
        ]
        if not rows:
            return pd.DataFrame(columns=["rule", "n_rejected"])
        return (
            pd.DataFrame(rows).groupby("rule", as_index=False)
            .size().rename(columns={"size": "n_rejected"})
            .sort_values("n_rejected", ascending=False).reset_index(drop=True)
        )


# --------------------------------------------------------------------------------------
# Metric computation
# --------------------------------------------------------------------------------------


def compute_metrics(
    prices: pd.DataFrame,
    shares: pd.DataFrame,
    securities: pd.DataFrame,
    as_of: dt.date,
    window_days: int,
    fx_rates: dict[str, float] | None = None,
) -> list[SecurityMetrics]:
    """Build screening inputs from raw tables, using only data known at `as_of`.

    The window ends at `as_of`, which is the review cut-off, not the effective date.
    The gap between them is what gives tracking funds time to trade, and computing
    metrics at the effective date instead would be a look-ahead of exactly that many
    days.

    `window_days` counts **trading** days, not calendar days. The distinction is not
    pedantry: 250 calendar days is about 174 sessions, so a calendar window paired with
    a 200-session presence requirement is unsatisfiable by construction and rejects the
    entire universe. Published methodologies specify sessions, and so does this.
    """
    fx_rates = fx_rates or {}
    session_dates = sorted({d for d in prices["date"].unique() if d <= as_of})
    if not session_dates:
        return []
    window = set(session_dates[-window_days:])
    px = prices[prices["date"].isin(window)]
    if px.empty:
        return []

    px = px.copy()
    px["fx"] = px["currency"].map(lambda c: fx_rates.get(str(c), 1.0))
    px["traded_value"] = px["close"] * px["volume"] * px["fx"]

    agg = px.groupby("security_id").agg(
        median_traded_value=("traded_value", "median"),
        observations=("close", "size"),
        last_close=("close", "last"),
        last_fx=("fx", "last"),
        currency=("currency", "last"),
        any_suspended=("is_suspended", "last"),
    )

    share_lookup = (
        shares[shares["knowledge_date"] <= as_of]
        .sort_values(["security_id", "effective_date", "knowledge_date"])
        .groupby("security_id").last()
    )
    sec_lookup = securities.set_index("security_id")

    out: list[SecurityMetrics] = []
    for sec_id, row in agg.iterrows():
        sid = str(sec_id)
        sh = share_lookup.loc[sid] if sid in share_lookup.index else None
        meta = sec_lookup.loc[sid] if sid in sec_lookup.index else None
        if sh is None or meta is None:
            continue

        float_factor = float(sh["free_float_factor"])
        fol = float(sh.get("foreign_ownership_limit", 1.0))
        investable = min(float_factor, fol)
        float_cap = (
            float(row["last_close"]) * float(sh["shares_outstanding"])
            * investable * float(row["last_fx"])
        )
        turnover_ratio = (
            float(row["median_traded_value"]) / float_cap if float_cap > 0 else 0.0
        )

        listing_start = meta.get("listing_start")
        age = (as_of - listing_start).days if isinstance(listing_start, dt.date) else 9999

        out.append(SecurityMetrics(
            security_id=sid,
            free_float_factor=float_factor,
            foreign_ownership_limit=fol,
            float_market_cap=float_cap,
            median_daily_turnover_ratio=turnover_ratio,
            price_observations=int(row["observations"]),
            security_type=SecurityType(str(meta["security_type"])),
            market_status=MarketStatus(str(meta["market_status"])),
            listing_age_days=age,
            is_suspended=bool(row["any_suspended"]),
        ))
    return out


def liquidity_percentile(metrics: list[SecurityMetrics]) -> dict[str, float]:
    """Cross-sectional liquidity rank, for diagnostics and for fast-entry tests."""
    values = np.array([m.median_daily_turnover_ratio for m in metrics])
    if values.size == 0:
        return {}
    ranks = values.argsort().argsort() / max(values.size - 1, 1)
    return {m.security_id: float(r) for m, r in zip(metrics, ranks, strict=False)}
