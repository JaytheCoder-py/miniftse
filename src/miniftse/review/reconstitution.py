"""Periodic reconstitution.

The review is where an index is actually designed. Everything else is arithmetic; this
is where the rules meet the market and someone has to decide what the index *is* this
quarter.

The calendar has three distinct dates and conflating any two is a real bug:

* **cut-off** - the last date whose data may influence the decision.
* **announcement** - when the changes are published.
* **effective** - when they enter the index.

The gap from cut-off to announcement is operational: screens run, exceptions get
reviewed, the committee signs off. The gap from announcement to effective is a
deliberate gift to the market - tracking funds need time to trade, and an index that
changed without warning would cost its own trackers money at every review.

Computing screens at the effective date rather than the cut-off is a look-ahead of
exactly that many days, and it is silent.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from miniftse.calc.index import ConstituentSpec
from miniftse.config import IndexConfig
from miniftse.types import Country, Currency, SizeBand
from miniftse.universe.banding import BandAssignment, assign_bands
from miniftse.universe.screens import (
    EligibilityScreener,
    ScreenReport,
    SecurityMetrics,
    compute_metrics,
)
from miniftse.weighting.capping import apply_ucits_5_10_40
from miniftse.weighting.schemes import SecurityInputs, float_market_cap_weights
from miniftse.weighting.weighters import FloatCapWeighter, Weighter, WeightingContext


@dataclass(frozen=True, slots=True)
class ReviewDates:
    """One review's three dates."""

    cutoff: dt.date
    announcement: dt.date
    effective: dt.date

    def describe(self) -> str:
        return (
            f"data to {self.cutoff}, announced {self.announcement}, "
            f"effective {self.effective} "
            f"({(self.effective - self.announcement).days} days' notice)"
        )


@dataclass
class ReviewOutcome:
    """The full record of one review. This is the audit artefact."""

    dates: ReviewDates
    constituents: dict[str, ConstituentSpec]
    screen_reports: dict[str, ScreenReport]
    band_assignments: dict[str, BandAssignment]
    raw_weights: dict[str, float]
    capped_weights: dict[str, float]
    additions: tuple[str, ...]
    deletions: tuple[str, ...]
    fast_entries: tuple[str, ...]
    turnover: float
    capping_notes: tuple[str, ...]
    n_held_by_buffer: int

    def summary(self) -> dict[str, object]:
        return {
            "effective": self.dates.effective,
            "cutoff": self.dates.cutoff,
            "n_constituents": len(self.constituents),
            "n_additions": len(self.additions),
            "n_deletions": len(self.deletions),
            "n_fast_entries": len(self.fast_entries),
            "one_way_turnover": self.turnover,
            "n_held_by_buffer": self.n_held_by_buffer,
            "max_weight": max(self.capped_weights.values(), default=0.0),
        }


def review_calendar(
    start: dt.date, end: dt.date, config: IndexConfig
) -> list[ReviewDates]:
    """Generate the review calendar.

    Effective dates are the third Friday of each review month, which is the usual
    convention: it coincides with derivatives expiry, so the trading needed to
    reposition meets the deepest liquidity of the quarter. Aligning the two is a
    deliberate courtesy to tracking funds, not a coincidence.
    """
    out: list[ReviewDates] = []
    review = config.review
    for year in range(start.year, end.year + 1):
        for month in review.months:
            effective = _third_friday(year, month)
            if not (start <= effective <= end):
                continue
            announcement = effective - dt.timedelta(days=review.announcement_lag_days)
            cutoff = announcement - dt.timedelta(days=review.cutoff_lag_days)
            out.append(ReviewDates(cutoff=cutoff, announcement=announcement,
                                   effective=effective))
    return sorted(out, key=lambda r: r.effective)


def _third_friday(year: int, month: int) -> dt.date:
    first = dt.date(year, month, 1)
    # weekday(): Monday is 0, Friday is 4.
    first_friday = first + dt.timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + dt.timedelta(days=14)


@dataclass
class ReconstitutionEngine:
    """Implements `UniverseSource`: produces the constituent set at each review.

    Stateful by necessity. Buffers and incumbent relief both depend on the previous
    review's outcome, so the engine has to remember what it decided last time - which
    is also why an index history cannot be recomputed from the middle. Rebuilding from
    the base date is the only reproducible option, and that constraint is exactly why
    the golden-master test exists.
    """

    config: IndexConfig
    prices: pd.DataFrame
    shares: pd.DataFrame
    securities: pd.DataFrame
    fx_rates: dict[str, float] = field(default_factory=dict)
    score_provider: object | None = None
    """Optional factor-score source for a factor variant. `None` gives the
    float-cap-weighted parent."""

    weighter: Weighter = field(default_factory=FloatCapWeighter)
    """How the candidate set becomes weights. Swapping this is the whole difference
    between the parent, a factor tilt, a selection index and an optimised variant -
    every screen, buffer, calendar and corporate-action rule is shared."""

    fund_size: float = 0.0
    """Assumed tracking-fund size, for capacity constraints. Zero disables them."""

    returns_window: int = 400
    """Trading days of return history handed to the optimised weighter's risk model."""

    _outcomes: dict[dt.date, ReviewOutcome] = field(default_factory=dict, repr=False)
    _previous_bands: dict[str, SizeBand] = field(default_factory=dict, repr=False)
    _previous_members: set[str] = field(default_factory=set, repr=False)
    _calendar: list[ReviewDates] = field(default_factory=list, repr=False)
    _meta: dict[str, dict[str, object]] = field(default_factory=dict, repr=False)
    _wide_returns: pd.DataFrame | None = field(default=None, repr=False)
    _weighter_diagnostics: dict[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._meta = {
            str(r.security_id): {
                "currency": Currency(str(r.currency)),
                "country": Country(str(r.country)),
                "icb_industry": str(r.icb_industry),
            }
            for r in self.securities.itertuples(index=False)
        }

    # ------------------------------------------------------------------ protocol

    def effective_dates(self, start: dt.date, end: dt.date) -> list[dt.date]:
        self._calendar = review_calendar(start, end, self.config)
        return [r.effective for r in self._calendar]

    def constituents_for(self, as_of: dt.date) -> dict[str, ConstituentSpec]:
        """Constituents effective on `as_of`.

        Reviews must be evaluated in order because each depends on the last. Asking for
        a later one first is a programming error, not something to paper over.
        """
        if as_of in self._outcomes:
            return self._outcomes[as_of].constituents

        dates = next((r for r in self._calendar if r.effective == as_of), None)
        if dates is None:
            # The base date, before any scheduled review: screen at the date itself.
            dates = ReviewDates(cutoff=as_of, announcement=as_of, effective=as_of)

        outcome = self.run_review(dates)
        self._outcomes[as_of] = outcome
        return outcome.constituents

    # ------------------------------------------------------------------ the review

    def run_review(self, dates: ReviewDates) -> ReviewOutcome:
        """Screen, band, weight, cap - in that order, using cut-off data only."""
        metrics = compute_metrics(
            prices=self.prices,
            shares=self.shares,
            securities=self.securities,
            as_of=dates.cutoff,
            window_days=self.config.eligibility.liquidity_window_days,
            fx_rates=self.fx_rates,
        )
        if not metrics:
            raise ValueError(f"no securities with data at the cut-off {dates.cutoff}")

        screener = EligibilityScreener(self.config.eligibility)
        reports = screener.screen_all(metrics, incumbents=self._previous_members)
        eligible = {m.security_id: m for m in metrics if reports[m.security_id].eligible}

        # --- size bands --------------------------------------------------------
        caps = {sid: m.float_market_cap for sid, m in eligible.items()}
        bands = assign_bands(caps, self.config.banding, self._previous_bands)
        wanted = set(self.config.size_bands)
        in_scope = {
            sid: m for sid, m in eligible.items() if str(bands[sid].band) in wanted
        }

        # --- fast entry --------------------------------------------------------
        fast = self._fast_entries(metrics, in_scope, caps, dates)
        for sid in fast:
            in_scope.setdefault(sid, next(m for m in metrics if m.security_id == sid))

        if not in_scope:
            # Which rule emptied the universe is the whole diagnosis, and recomputing it
            # from a bare "selected nothing" costs an afternoon. The common cause is a
            # build whose base date is the first day of its own price history: every
            # security then fails `price_history`, because there is none behind the
            # cut-off to measure.
            rejections = EligibilityScreener.rejection_summary(reports)
            by_rule = ", ".join(
                f"{r.rule} {r.n_rejected}" for r in rejections.itertuples(index=False)
            ) or "none - every security was screened in but fell outside the size bands"
            raise ValueError(
                f"review at {dates.effective} selected nothing: {len(metrics)} securities "
                f"had data at the cut-off {dates.cutoff}, {len(eligible)} passed the "
                f"screens, {len(in_scope)} landed in bands {sorted(wanted)}. "
                f"Rejected by rule: {by_rule}."
            )

        # --- weights -----------------------------------------------------------
        inputs = {
            sid: SecurityInputs(
                security_id=sid,
                price=1.0,
                shares=m.float_market_cap,   # cap already includes float and FX
                free_float_factor=1.0,
                score=self._score(sid, dates.cutoff),
                # Absolute traded value, not the ratio the liquidity screen uses. The
                # capacity constraint asks "how many days would a fund of size X take
                # to build this position", which needs a level, not a proportion.
                adv=m.median_daily_traded_value,
                volatility=m.realised_volatility,
                fundamental_size=m.float_market_cap,
            )
            for sid, m in in_scope.items()
        }
        context = WeightingContext(
            as_of=dates.cutoff,
            previous_weights=dict(self._last_weights),
            parent_weights=float_market_cap_weights(inputs),
            returns=self._returns_to(dates.cutoff, list(in_scope)),
            industry={sid: str(self._meta.get(sid, {}).get("icb_industry", "?"))
                      for sid in in_scope},
            country={sid: str(self._meta.get(sid, {}).get("country", "?"))
                     for sid in in_scope},
            fund_size=self.fund_size,
        )
        # Float-cap weights on the same candidate set: the denominator the index
        # engine implicitly applies, and therefore the baseline C_i must be measured
        # against. Computed before the weighter narrows the set.
        float_cap = float_market_cap_weights(inputs)
        raw = self.weighter.weights(inputs, context)
        # A weighter may drop securities entirely - a selection index holds the top
        # decile, not the candidate set - so the constituent set is what came back, not
        # what went in.
        in_scope = {sid: m for sid, m in in_scope.items() if sid in raw}
        capped = apply_ucits_5_10_40(raw, self.config.capping)
        self._weighter_diagnostics = self.weighter.diagnostics()

        # --- assemble ----------------------------------------------------------
        share_lookup = (
            self.shares[self.shares["knowledge_date"] <= dates.cutoff]
            .sort_values(["security_id", "effective_date", "knowledge_date"])
            .groupby("security_id").last()
        )
        constituents: dict[str, ConstituentSpec] = {}
        for sid in in_scope:
            if sid not in share_lookup.index:
                continue
            sh = share_lookup.loc[sid]
            meta = self._meta.get(sid, {})
            metric = in_scope[sid]
            # The weighting factor C_i, relative to FLOAT-CAP weight - not to the
            # weighter's own output.
            #
            # The index computes weights as P*S*F*C / sum(P*S*F*C), and P*S*F is already
            # the float-cap weight. So C must carry the *entire* deviation from cap
            # weighting: C_i = target_i / floatcap_i. Setting C = capped/raw instead
            # gives C ~ 1 whenever capping is not binding, and the published index
            # silently reverts to float-cap weights however the weighter was configured.
            #
            # That is what happened here: a strength-1.0 value tilt with 0.40 active
            # share at the review published an index with 0.003 active share against
            # its parent. The weighter, the scores and the capping were all correct;
            # the factor that carried them into the index was not.
            factor = (
                capped.weights[sid] / float_cap[sid]
                if float_cap.get(sid) else 1.0
            )
            constituents[sid] = ConstituentSpec(
                security_id=sid,
                shares=float(sh["shares_outstanding"]),
                free_float_factor=min(
                    float(sh["free_float_factor"]),
                    float(sh.get("foreign_ownership_limit", 1.0)),
                ),
                capping_factor=factor,
                size_band=bands[sid].band if sid in bands else SizeBand.LARGE,
                currency=meta.get("currency", Currency.USD),  # type: ignore[arg-type]
                country=meta.get("country", Country.US),  # type: ignore[arg-type]
                icb_industry=str(meta.get("icb_industry", "")),
                adv=metric.median_daily_traded_value,
            )

        additions = tuple(sorted(set(constituents) - self._previous_members))
        deletions = tuple(sorted(self._previous_members - set(constituents)))
        turnover = self._turnover(capped.weights)

        self._previous_bands = {sid: a.band for sid, a in bands.items()}
        self._previous_members = set(constituents)
        self._last_weights = dict(capped.weights)

        return ReviewOutcome(
            dates=dates, constituents=constituents, screen_reports=reports,
            band_assignments=bands, raw_weights=raw, capped_weights=capped.weights,
            additions=additions, deletions=deletions, fast_entries=tuple(fast),
            turnover=turnover, capping_notes=capped.notes,
            n_held_by_buffer=sum(1 for a in bands.values() if a.held_by_buffer),
        )

    # ------------------------------------------------------------------ helpers

    _last_weights: dict[str, float] = field(default_factory=dict, repr=False)

    def _returns_to(self, cutoff: dt.date, ids: list[str]) -> pd.DataFrame | None:
        """Trailing daily returns ending at the cut-off, for the optimised weighter.

        Ends at the **cut-off**, not the effective date. Using returns up to the
        effective date would give the risk model two to five weeks of foresight at every
        review, and the resulting index would show a tracking error it could never have
        achieved live.
        """
        if self._wide_returns is None:
            wide = self.prices.pivot_table(
                index="date", columns="security_id", values="close", aggfunc="last"
            ).sort_index()
            self._wide_returns = wide.pct_change()
        frame = self._wide_returns
        window = frame.loc[frame.index <= cutoff].tail(self.returns_window)
        available = [c for c in ids if c in window.columns]
        return window[available] if available else None

    def _score(self, security_id: str, as_of: dt.date) -> float:
        if self.score_provider is None:
            return 0.0
        getter = getattr(self.score_provider, "score", None)
        return float(getter(security_id, as_of)) if getter else 0.0

    def _fast_entries(
        self,
        metrics: list[SecurityMetrics],
        in_scope: dict[str, SecurityMetrics],
        caps: dict[str, float],
        dates: ReviewDates,
    ) -> list[str]:
        """Large recent listings admitted off the normal size-band path.

        Fast entry exists because a very large IPO left out until the next scheduled
        review makes the index unrepresentative for months, and every fund tracking it
        carries an unintended underweight. The size bar is high on purpose: the
        exception must be rarer than the rule, or the review calendar stops meaning
        anything.
        """
        review = self.config.review
        if not review.fast_entry_enabled or not caps:
            return []

        threshold_cap = pd.Series(list(caps.values())).quantile(
            review.fast_entry_min_percentile
        )
        out: list[str] = []
        for m in metrics:
            if m.security_id in in_scope:
                continue
            if m.listing_age_days > review.fast_entry_max_listing_age_days:
                continue  # not new; it simply failed the normal screens
            if m.float_market_cap >= threshold_cap and m.investable_factor >= (
                self.config.eligibility.min_free_float_developed
            ):
                out.append(m.security_id)
        del dates
        return out

    def _turnover(self, new_weights: dict[str, float]) -> float:
        if not self._last_weights:
            return 0.0
        keys = set(self._last_weights) | set(new_weights)
        return sum(
            abs(new_weights.get(k, 0.0) - self._last_weights.get(k, 0.0)) for k in keys
        ) / 2.0

    # ------------------------------------------------------------------ reporting

    def outcomes_frame(self) -> pd.DataFrame:
        if not self._outcomes:
            return pd.DataFrame()
        return pd.DataFrame([o.summary() for o in self._outcomes.values()])

    def turnover_attribution(self, effective: dt.date) -> dict[str, float]:
        """Split one review's turnover into its causes.

        Additions and deletions, versus reweighting of survivors. The components sum to
        the total, which is the acceptance criterion - a turnover figure that cannot be
        decomposed is a number nobody can act on, and "why did the index turn over 4.2%
        in June?" is a question with a real answer.
        """
        outcome = self._outcomes.get(effective)
        if outcome is None:
            raise KeyError(f"no review recorded for {effective}")

        prior = sorted(d for d in self._outcomes if d < effective)
        if not prior:
            return {"total": 0.0, "additions": 0.0, "deletions": 0.0, "reweighting": 0.0}

        before = self._outcomes[prior[-1]].capped_weights
        after = outcome.capped_weights
        adds = sum(after.get(k, 0.0) for k in after if k not in before)
        dels = sum(before.get(k, 0.0) for k in before if k not in after)
        survivors = set(before) & set(after)
        drift = sum(abs(after[k] - before[k]) for k in survivors) / 2.0

        return {
            "total": (adds + dels) / 2.0 + drift,
            "additions": adds / 2.0,
            "deletions": dels / 2.0,
            "reweighting": drift,
            "n_additions": float(len(set(after) - set(before))),
            "n_deletions": float(len(set(before) - set(after))),
        }
