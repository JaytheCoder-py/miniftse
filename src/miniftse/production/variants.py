"""Index variants: parent, factor tilt, selection, optimised, capacity-constrained,
and currency-hedged.

Every variant shares the same universe, screens, buffers, review calendar and
corporate-action treatment. They differ only in how the candidate set becomes weights,
and — for the hedged variant — in an overlay applied after the index is calculated.

That is the point of the design. A new product is a `Weighter` and a config, not a fork
of the calculation engine, and the shared machinery means a fix to the divisor logic
lands in every product at once.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

from miniftse.calc.fx import FxTable, HedgedIndexCalculator, currency_exposures
from miniftse.calc.index import IndexCalculator, IndexHistory
from miniftse.config import IndexConfig, global_all_cap
from miniftse.corpactions.engine import CorporateActionEngine
from miniftse.data.providers import UniverseData
from miniftse.data.synthetic import SyntheticConfig, SyntheticUniverse
from miniftse.factors.build import FactorInputBuilder
from miniftse.factors.scores import FactorScoreProvider
from miniftse.production.manifest import RunManifest
from miniftse.review.reconstitution import ReconstitutionEngine
from miniftse.weighting.weighters import (
    CapacityConstrainedWeighter,
    FloatCapWeighter,
    OptimisedWeighter,
    SelectionWeighter,
    TiltWeighter,
)


@dataclass
class VariantSpec:
    """One index variant."""

    variant_id: str
    name: str
    weighter_kind: str = "float_cap"
    """float_cap | tilt | selection | optimised"""

    factor: str | None = None
    tilt_strength: float = 1.0
    top_fraction: float = 0.30
    tracking_error_limit: float = 0.03
    sector_deviation: float = 0.05
    max_turnover: float = 0.20
    fund_size: float = 0.0
    """Non-zero enables the capacity constraint."""

    max_days_to_trade: float = 5.0
    hedged: bool = False
    hedge_ratio: float = 1.0
    composite: dict[str, float] | None = None
    index_config: IndexConfig | None = None

    def describe(self) -> str:
        bits = [f"{self.name} ({self.variant_id})", f"weighting: {self.weighter_kind}"]
        if self.factor:
            bits.append(f"factor: {self.factor}")
        if self.composite:
            bits.append(
                "composite: " + ", ".join(f"{k} {v:.0%}" for k, v in self.composite.items())
            )
        if self.weighter_kind == "tilt":
            bits.append(f"tilt strength {self.tilt_strength:g}")
        if self.weighter_kind == "selection":
            bits.append(f"top {self.top_fraction:.0%}")
        if self.weighter_kind == "optimised":
            bits.append(
                f"TE cap {self.tracking_error_limit:.1%}, "
                f"sector +/-{self.sector_deviation:.1%}, "
                f"turnover {self.max_turnover:.0%}"
            )
        if self.fund_size:
            bits.append(
                f"capacity for {self.fund_size / 1e9:.0f}bn at {self.max_days_to_trade:g} days"
            )
        if self.hedged:
            bits.append(f"currency hedged at {self.hedge_ratio:.0%}")
        return " | ".join(bits)


@dataclass
class VariantResult:
    spec: VariantSpec
    history: IndexHistory
    reconstitution: ReconstitutionEngine
    calculator: IndexCalculator
    manifest: RunManifest
    score_provider: FactorScoreProvider | None
    weighter_diagnostics: dict[str, Any]
    hedge: pd.DataFrame | None = None
    duration: float = 0.0

    def summary(self) -> dict[str, Any]:
        out = dict(self.history.summary())
        out["variant"] = self.spec.variant_id
        out["weighting"] = self.spec.weighter_kind
        reviews = self.history.reviews
        out["mean_review_turnover"] = (
            float(reviews["one_way_turnover"].mean()) if not reviews.empty else 0.0
        )
        out["annual_turnover"] = out["mean_review_turnover"] * len(
            self.history.config.review.months
        )
        out.update(
            {k: v for k, v in self.weighter_diagnostics.items() if isinstance(v, int | float | str)}
        )
        return out


# --------------------------------------------------------------------------------------


@dataclass
class VariantBuilder:
    """Builds several variants over one shared universe.

    The universe is generated once and reused. It is the expensive part, and building it
    per variant would make a five-variant comparison five times slower for no benefit -
    and would also make the variants incomparable if any seed drifted.
    """

    universe_config: SyntheticConfig = field(default_factory=SyntheticConfig)
    start: dt.date = dt.date(2016, 1, 4)
    end: dt.date = dt.date(2026, 6, 30)
    base_config: IndexConfig = field(default_factory=global_all_cap)

    universe_data: UniverseData | None = None
    """Build every variant from this universe instead of generating one.

    All variants must share one universe - that is the whole point of the comparison,
    since active share and factor exposure are only meaningful against a parent built
    from the same securities. See `production.build.BuildSpec.universe`."""

    _universe: UniverseData | None = field(default=None, repr=False)
    _fx: FxTable | None = field(default=None, repr=False)
    _spot: dict[str, float] = field(default_factory=dict, repr=False)
    _builder: FactorInputBuilder | None = field(default=None, repr=False)

    @property
    def universe(self) -> UniverseData:
        if self._universe is None:
            self._universe = self.universe_data or SyntheticUniverse(self.universe_config)
        return self._universe

    @property
    def fx(self) -> FxTable:
        if self._fx is None:
            u = self.universe
            quotes = list(u.fx_rates["quote"].unique())
            self._fx = FxTable.from_frame(
                u.get_fx("USD", quotes, u.start, u.end),
                u.get_deposit_rates(quotes, u.start, u.end),
                base=str(self.base_config.base_currency),
            )
        return self._fx

    @property
    def spot(self) -> dict[str, float]:
        if not self._spot:
            self._spot = {c: self.fx.rate(self.start, c) for c in self.fx.currencies()}
        return self._spot

    @property
    def factor_builder(self) -> FactorInputBuilder:
        if self._builder is None:
            self._builder = FactorInputBuilder(provider=self.universe)
        return self._builder

    # ------------------------------------------------------------------

    def build(self, spec: VariantSpec, verbose: bool = False) -> VariantResult:
        started = time.perf_counter()
        log = print if verbose else (lambda *a, **k: None)
        config = spec.index_config or replace(
            self.base_config, index_id=spec.variant_id, name=spec.name
        )

        universe = self.universe
        prices = universe.prices

        manifest = RunManifest.start(config.index_id, self.end, config)
        manifest.record_input("prices", prices)
        manifest.record_input(
            "universe", {"name": universe.name, "fingerprint": universe.fingerprint}
        )
        manifest.record_input("variant", spec.describe())

        score_provider: FactorScoreProvider | None = None
        if spec.factor or spec.composite:
            score_provider = FactorScoreProvider(
                builder=self.factor_builder,
                factor=spec.factor or "value",
                tilt_strength=spec.tilt_strength,
                fx_rates=self.spot,
                composite_weights=spec.composite,
            )

        weighter = self._make_weighter(spec)
        log(f"  building {spec.variant_id}: {spec.describe()}")

        reconstitution = ReconstitutionEngine(
            config=config,
            prices=prices,
            shares=universe.shares,
            securities=universe.get_securities(),
            fx_rates=self.spot,
            score_provider=score_provider,
            weighter=weighter,
            fund_size=spec.fund_size,
        )
        engine = CorporateActionEngine(
            withholding_tax={str(k): v for k, v in config.withholding_tax.items()}
        )
        calculator = IndexCalculator(config=config, fx=self.fx, engine=engine)
        history = calculator.run(
            prices,
            universe.corp_actions,
            reconstitution,
            self.start,
            self.end,
        )

        hedge: pd.DataFrame | None = None
        if spec.hedged:
            log("    applying the currency hedge overlay")
            hedge = self._apply_hedge(history, spec, config)

        manifest.record_output("levels", history.levels)
        manifest.record_output("weights", history.weights)
        for key, value in history.summary().items():
            manifest.record_metric(key, value)
        manifest.finish("success", time.perf_counter() - started)

        return VariantResult(
            spec=spec,
            history=history,
            reconstitution=reconstitution,
            calculator=calculator,
            manifest=manifest,
            score_provider=score_provider,
            weighter_diagnostics=dict(reconstitution._weighter_diagnostics),
            hedge=hedge,
            duration=time.perf_counter() - started,
        )

    @staticmethod
    def _make_weighter(spec: VariantSpec) -> Any:
        match spec.weighter_kind:
            case "float_cap":
                weighter: Any = FloatCapWeighter()
            case "tilt":
                weighter = TiltWeighter(strength=spec.tilt_strength)
            case "selection":
                weighter = SelectionWeighter(top_fraction=spec.top_fraction)
            case "optimised":
                config = spec.index_config or global_all_cap()
                weighter = OptimisedWeighter(
                    tracking_error_limit=spec.tracking_error_limit,
                    sector_deviation=spec.sector_deviation,
                    max_turnover=spec.max_turnover,
                    max_weight=config.capping.max_single_weight,
                    capping_config=config.capping,
                )
            case _:
                raise ValueError(f"unknown weighter {spec.weighter_kind!r}")

        if spec.fund_size:
            weighter = CapacityConstrainedWeighter(
                inner=weighter,
                fund_size=spec.fund_size,
                max_days_to_trade=spec.max_days_to_trade,
            )
        return weighter

    # ------------------------------------------------------------------ hedging

    def _apply_hedge(
        self, history: IndexHistory, spec: VariantSpec, config: IndexConfig
    ) -> pd.DataFrame:
        """Overlay a monthly-reset currency hedge on a computed index history.

        Applied *after* the index, not inside it, because that is what a hedged index
        is: the same index plus a forward overlay. Building it as a separate calculation
        would let the two drift apart, and the hedged and unhedged series must be
        reconcilable line by line — the first client question is always "why is the
        difference not just the interest rate differential?"
        """
        hedger = HedgedIndexCalculator(fx=self.fx, hedge_ratio=spec.hedge_ratio)
        levels = history.levels
        weights = history.weights

        # Currency composition changes only at review and month end, which is when
        # weights are snapshotted. Between snapshots the composition drifts with prices,
        # and that drift is precisely the hedge error the decomposition reports.
        currency_of = {
            str(r.security_id): str(r.currency)
            for r in self.universe.get_securities().itertuples(index=False)
        }

        rows: list[dict[str, Any]] = []
        hedged_level = float(levels.iloc[0]["gross_total_return"])
        prev_unhedged = float(levels.iloc[0]["gross_total_return"])
        snapshot_dates = sorted(weights["date"].unique()) if not weights.empty else []
        current: dict[str, float] = {}
        prior_pnl = 0.0

        for row in levels.itertuples(index=False):
            date = row.date
            if snapshot_dates and date in snapshot_dates:
                snap = weights[weights["date"] == date]
                market_values = {
                    str(r.security_id): float(r.weight) * float(row.total_market_value)
                    for r in snap.itertuples(index=False)
                }
                current = currency_exposures(market_values, currency_of, self.fx, date)

            if current and hedger.should_reset(date):
                # Closing and re-striking crystallises the P&L to date, so the new
                # position starts from zero and the baseline must reset with it.
                hedger.reset(date, current)
                prior_pnl = 0.0

            pnl = hedger.mark(date) if current else 0.0
            unhedged = float(row.gross_total_return)
            market_value = float(row.total_market_value) or 1.0

            # The DAILY CHANGE in the forward's mark, not its cumulative value.
            #
            # Adding the running mark-to-market every day compounds the same profit
            # repeatedly: it more than doubled the hedged index over six years while the
            # carry it claimed to be earning was 14%. The forward's contribution to
            # today's return is what it gained today.
            daily_pnl = pnl - prior_pnl
            prior_pnl = pnl

            hedge_return = daily_pnl / market_value
            unhedged_return = (unhedged / prev_unhedged - 1.0) if prev_unhedged else 0.0
            hedged_level *= 1.0 + unhedged_return + hedge_return
            prev_unhedged = unhedged

            decomposition = (
                hedger.decompose(date, current)
                if current
                else {"carry": 0.0, "hedge_error": 0.0, "total_hedge_pnl": 0.0}
            )
            rows.append(
                {
                    "date": date,
                    "unhedged_gtr": unhedged,
                    "hedged_gtr": hedged_level,
                    "hedge_return": hedge_return,
                    # Carry and hedge error are levels of the current position, not daily
                    # increments, so they are reported as a decomposition of the open
                    # position rather than summed across days.
                    "position_carry": decomposition["carry"] / market_value,
                    "position_hedge_error": decomposition["hedge_error"] / market_value,
                    "cumulative_pnl": pnl / market_value,
                    "n_legs": len(hedger._legs),
                }
            )
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# Standard variant set
# --------------------------------------------------------------------------------------


def standard_variants(factor: str = "value") -> list[VariantSpec]:
    """The comparison the plan calls "three roads to a value index", plus the parent.

    All four hold the same universe under the same rules. The differences are entirely
    attributable to the weighting decision, which is what makes the comparison mean
    anything.
    """
    return [
        VariantSpec("MFTSE-GLOBAL", "miniFTSE Global All Cap", "float_cap"),
        VariantSpec(
            f"MFTSE-{factor.upper()}-SEL",
            f"miniFTSE Global {factor.title()} (Selection)",
            "selection",
            factor=factor,
            top_fraction=0.30,
        ),
        VariantSpec(
            f"MFTSE-{factor.upper()}-TILT",
            f"miniFTSE Global {factor.title()} (Tilt)",
            "tilt",
            factor=factor,
            tilt_strength=1.0,
        ),
        VariantSpec(
            f"MFTSE-{factor.upper()}-OPT",
            f"miniFTSE Global {factor.title()} (Optimised)",
            "optimised",
            factor=factor,
            tracking_error_limit=0.03,
            sector_deviation=0.05,
            max_turnover=0.20,
        ),
    ]


def compare_variants(results: list[VariantResult]) -> pd.DataFrame:
    """The one-page comparison table.

    Factor exposure against turnover, capacity and explainability. No scheme wins on all
    three, and which two you pick is a product decision rather than a quantitative one.
    """
    parent = next((r for r in results if r.spec.weighter_kind == "float_cap"), None)
    rows: list[dict[str, Any]] = []

    # Factor exposure and active share are computed HERE, from the final index weights,
    # rather than taken from each weighter's own diagnostic.
    #
    # The weighters measure against whatever benchmark they happen to hold internally -
    # the tilt against the uncapped parent, the optimiser against the capped one - and
    # comparing those numbers across variants is meaningless. The first version of this
    # table reported the optimised index as having *half* the factor exposure of the
    # tilt while explicitly maximising exposure at a higher tracking-error budget, which
    # is not a result, it is a units error.
    scorer = next((r.score_provider for r in results if r.score_provider), None)
    parent_weights_by_date = (
        {
            d: g.set_index("security_id")["weight"].to_dict()
            for d, g in parent.history.weights.groupby("date")
        }
        if parent is not None and not parent.history.weights.empty
        else {}
    )

    for result in results:
        levels = result.history.levels
        reviews = result.history.reviews
        gtr = levels["gross_total_return"]
        years = (levels["date"].iloc[-1] - levels["date"].iloc[0]).days / 365.25
        ann = float((gtr.iloc[-1] / gtr.iloc[0]) ** (1 / years) - 1) if years else 0.0
        vol = float(gtr.pct_change().std() * np.sqrt(252))

        active_te = float("nan")
        active_return = float("nan")
        if parent is not None and result is not parent:
            merged = levels[["date", "gross_total_return"]].merge(
                parent.history.levels[["date", "gross_total_return"]],
                on="date",
                suffixes=("", "_parent"),
            )
            active = (
                merged["gross_total_return"].pct_change()
                - merged["gross_total_return_parent"].pct_change()
            ).dropna()
            active_te = float(active.std() * np.sqrt(252))
            active_return = ann - float(
                (
                    parent.history.levels["gross_total_return"].iloc[-1]
                    / parent.history.levels["gross_total_return"].iloc[0]
                )
                ** (1 / years)
                - 1
            )

        turnover = (
            float(reviews["one_way_turnover"].mean()) * len(result.history.config.review.months)
            if not reviews.empty
            else 0.0
        )
        diagnostics = result.weighter_diagnostics
        exposure, active_share = _exposure_and_active_share(result, parent_weights_by_date, scorer)

        rows.append(
            {
                "variant": result.spec.variant_id,
                "weighting": result.spec.weighter_kind,
                "n_constituents": float(levels["n_constituents"].mean()),
                "ann_return": ann,
                "ann_vol": vol,
                "max_drawdown": result.history.max_drawdown(),
                "active_return": active_return,
                "tracking_error": active_te,
                "information_ratio": (
                    active_return / active_te if active_te and np.isfinite(active_te) else np.nan
                ),
                "annual_turnover": turnover,
                "active_share": active_share,
                "factor_exposure": exposure,
                "max_weight": float(result.history.weights.groupby("date")["weight"].max().mean())
                if not result.history.weights.empty
                else np.nan,
                "days_to_trade": diagnostics.get("weighted_days_to_trade_after", np.nan),
                "explainability": {
                    "float_cap": "highest - the market",
                    "tilt": "high - cap weight scaled by score",
                    "selection": "high - the best 30% by score",
                    "optimised": "lowest - constrained optimisation",
                }[result.spec.weighter_kind],
            }
        )
    return pd.DataFrame(rows)


def _exposure_and_active_share(
    result: VariantResult,
    parent_weights_by_date: dict[Any, dict[str, float]],
    scorer: FactorScoreProvider | None,
) -> tuple[float, float]:
    """Mean active factor exposure and active share, from the published index weights.

    Averaged across weight snapshots rather than taken at a single date, because a
    single date can land just after a review when exposure is at its peak. What a client
    experiences is the average.
    """
    weights = result.history.weights
    if weights.empty or not parent_weights_by_date:
        return float("nan"), float("nan")

    exposures: list[float] = []
    shares: list[float] = []
    for date, group in weights.groupby("date"):
        parent = parent_weights_by_date.get(date)
        if not parent:
            continue
        own = group.set_index("security_id")["weight"].to_dict()
        keys = set(own) | set(parent)
        shares.append(sum(abs(own.get(k, 0.0) - parent.get(k, 0.0)) for k in keys) / 2.0)
        if scorer is not None:
            panel = scorer.scores_at(_nearest_scored_date(scorer, date))
            if not panel.empty:
                exposures.append(
                    sum(w * float(panel.get(k, 0.0)) for k, w in own.items())
                    - sum(w * float(panel.get(k, 0.0)) for k, w in parent.items())
                )
    return (
        float(np.mean(exposures)) if exposures else float("nan"),
        float(np.mean(shares)) if shares else float("nan"),
    )


def _nearest_scored_date(scorer: FactorScoreProvider, date: Any) -> Any:
    """The most recent cut-off at which scores were computed.

    Weight snapshots fall on month ends; scores are computed at review cut-offs. Asking
    the provider for a month-end would compute a fresh cross-section - correct, but it
    would also silently use data the index never saw, and it would multiply the cost of
    this table by the number of month ends.
    """
    computed = [d for d in scorer._cache if d <= date]
    return max(computed) if computed else date


def recommend_variant(
    comparison: pd.DataFrame, max_turnover: float = 0.15, client: str = "a UK pension fund"
) -> str:
    """Turn the comparison into a recommendation for a named client type.

    A comparison table without a recommendation is homework. The job is to say which one
    and why, and to name the constraint that decided it.
    """
    eligible = comparison[
        (comparison["weighting"] != "float_cap") & (comparison["annual_turnover"] <= max_turnover)
    ]
    if eligible.empty:
        cheapest = comparison[comparison["weighting"] != "float_cap"].nsmallest(
            1, "annual_turnover"
        )
        if cheapest.empty:
            return "No factor variant was built, so there is nothing to recommend."
        row = cheapest.iloc[0]
        return (
            f"No variant meets a {max_turnover:.0%} annual turnover budget. The closest "
            f"is **{row['variant']}** at {row['annual_turnover']:.1%}. For {client} I "
            "would either raise the budget or lower the tilt strength rather than "
            "recommend a variant that breaches a stated constraint."
        )

    best = eligible.loc[eligible["factor_exposure"].idxmax()]
    return (
        f"For {client} seeking cheap factor exposure within a {max_turnover:.0%} annual "
        f"turnover budget, I recommend **{best['variant']}**.\n\n"
        f"It delivers {best['factor_exposure']:.3f} of active factor exposure and "
        f"{best['active_share']:.1%} active share for {best['annual_turnover']:.1%} "
        f"annual one-way turnover, at {best['tracking_error']:.2%} tracking error.\n\n"
        f"Explainability: {best['explainability']}. That matters more than it sounds — "
        "the variant with the highest exposure per unit of tracking error is the "
        'optimised one, and I am not recommending it, because "the optimiser did it" '
        "is not an answer a trustee board can act on. The tilt buys slightly less "
        "exposure for a story that survives a governance meeting."
    )
