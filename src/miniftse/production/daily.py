"""The real daily production job.

`production.pipeline` provides the DAG machinery — dependencies, per-step retry policy,
the gate as a node. This module supplies the actual steps, so `miniftse daily` runs the
genuine calculation rather than a set of passthrough placeholders.

The shape of a real daily job, and the reason it is not simply "rebuild everything":

* **Load** the day's market data, corporate actions and FX.
* **Validate the inputs** before computing anything. Cheaper to fail here, and the
  findings point at the vendor rather than at the index.
* **Roll the index forward one day** from yesterday's published state. A production
  index does not rebuild ten years of history every morning; it applies one day of
  prices and corporate actions to a stored state, which is why the divisor is
  persisted and why reproducibility needs a manifest rather than a rerun.
* **Validate the output**, then the **gate**, then publish.

The state file is the part people underestimate. Because the calculation is
incremental, a corrupted or missing state file means the only recovery is a full
rebuild from the base date — which is exactly why the golden master exists.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from miniftse.calc.fx import FxTable
from miniftse.calc.index import IndexCalculator, _PriceBook
from miniftse.calc.state import Constituent, IndexState
from miniftse.config import IndexConfig, global_all_cap
from miniftse.corpactions.engine import CorporateActionEngine
from miniftse.corpactions.events import parse_events
from miniftse.data.synthetic import SyntheticConfig, SyntheticUniverse
from miniftse.production.manifest import ManifestStore, RunManifest
from miniftse.production.pipeline import (
    DataNotReadyError,
    Pipeline,
    PipelineError,
    PipelineRun,
    StepResult,
    ValidationFailedError,
    build_daily_pipeline,
)
from miniftse.quality.rules import PublicationGate, ValidationContext
from miniftse.types import Country, Currency, SizeBand


@dataclass
class IndexStateFile:
    """The persisted end-of-day state the next morning resumes from."""

    index_id: str
    as_of: str
    divisor: float
    level_pr: float
    level_gtr: float
    level_ntr: float
    constituents: dict[str, dict[str, Any]]

    @classmethod
    def from_state(cls, index_id: str, state: IndexState, pr: float, gtr: float,
                   ntr: float) -> IndexStateFile:
        return cls(
            index_id=index_id, as_of=state.date.isoformat(), divisor=state.divisor,
            level_pr=pr, level_gtr=gtr, level_ntr=ntr,
            constituents={
                k: {
                    "price": c.price, "shares": c.shares,
                    "free_float_factor": c.free_float_factor,
                    "capping_factor": c.capping_factor, "fx_rate": c.fx_rate,
                    "currency": str(c.currency), "country": str(c.country),
                    "icb_industry": c.icb_industry, "size_band": str(c.size_band),
                    "adv": c.adv,
                }
                for k, c in state.constituents.items()
            },
        )

    def to_state(self) -> IndexState:
        return IndexState(
            date=dt.date.fromisoformat(self.as_of), divisor=self.divisor,
            constituents={
                k: Constituent(
                    security_id=k, price=v["price"], shares=v["shares"],
                    free_float_factor=v["free_float_factor"],
                    capping_factor=v["capping_factor"], fx_rate=v["fx_rate"],
                    currency=Currency(v["currency"]), country=Country(v["country"]),
                    icb_industry=v["icb_industry"], size_band=SizeBand(v["size_band"]),
                    adv=v.get("adv", 0.0),
                )
                for k, v in self.constituents.items()
            },
        )

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.index_id}_state.json"
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, directory: Path, index_id: str) -> IndexStateFile | None:
        path = Path(directory) / f"{index_id}_state.json"
        if not path.exists():
            return None
        return cls(**json.loads(path.read_text(encoding="utf-8")))


@dataclass
class DailyJob:
    """One index's daily production run, as a real DAG."""

    config: IndexConfig = field(default_factory=global_all_cap)
    universe_config: SyntheticConfig = field(default_factory=SyntheticConfig)
    state_dir: Path = Path("artefacts/state")
    manifest_dir: Path = Path("artefacts/manifests")
    simulate: str | None = None
    """Failure injection: 'late_data' | 'outlier' | 'missing_corp_action' | None."""

    _universe: SyntheticUniverse | None = field(default=None, repr=False)
    _fx: FxTable | None = field(default=None, repr=False)
    _attempts: int = field(default=0, repr=False)

    @property
    def universe(self) -> SyntheticUniverse:
        if self._universe is None:
            self._universe = SyntheticUniverse(self.universe_config)
        return self._universe

    @property
    def fx(self) -> FxTable:
        if self._fx is None:
            u = self.universe
            quotes = list(u._fx["quote"].unique())
            self._fx = FxTable.from_frame(
                u.get_fx("USD", quotes, u.config.start, u.config.end),
                u.get_deposit_rates(quotes, u.config.start, u.config.end),
                base=str(self.config.base_currency),
            )
        return self._fx

    # ------------------------------------------------------------------ steps

    def load_market_data(self, context: dict[str, Any]) -> dict[str, Any]:
        run_date: dt.date = context["run_date"]
        self._attempts += 1

        # Late data is the single most common overnight failure. It is retryable, and
        # the DAG's retry policy exists for exactly this - which is why the simulation
        # fires on the first two attempts and then succeeds.
        if self.simulate == "late_data" and self._attempts <= 2:
            raise DataNotReadyError(
                f"market data file for {run_date} has not arrived "
                f"(attempt {self._attempts})"
            )

        prices = self.universe.get_prices(None, run_date, run_date)
        if prices.empty:
            raise DataNotReadyError(f"no prices for {run_date} - not a trading day?")

        prior_dates = sorted(
            d for d in self.universe._generated["prices"]["date"].unique()
            if d < run_date
        )
        if not prior_dates:
            raise PipelineError(f"{run_date} has no prior session to roll from")
        prior = self.universe.get_prices(None, prior_dates[-1], prior_dates[-1])

        corp_actions = self.universe.get_corp_actions(None, run_date, run_date)
        if self.simulate == "missing_corp_action" and not corp_actions.empty:
            # The event file arrived but one row is missing. The index will be wrong by
            # that dividend, silently, unless a check catches it.
            corp_actions = corp_actions.iloc[1:]

        quotes = list(self.universe._fx["quote"].unique())
        return {
            "prices": prices,
            "prior_prices": prior,
            "prior_date": prior_dates[-1],
            "corp_actions": corp_actions,
            "all_corp_actions": self.universe.get_corp_actions(None, run_date, run_date),
            "fx": self.universe.get_fx("USD", quotes, run_date, run_date),
            "prior_fx": self.universe.get_fx("USD", quotes, prior_dates[-1],
                                             prior_dates[-1]),
            "shares": self.universe.get_shares(None, run_date),
        }

    def validate_inputs(self, context: dict[str, Any]) -> dict[str, Any]:
        data = context["load_market_data"]
        run_date: dt.date = context["run_date"]

        prices = data["prices"].copy()
        if self.simulate == "outlier":
            # A single price off by a factor of ten, on a name large enough to matter.
            idx = prices["close"].idxmax()
            prices.loc[idx, "close"] *= 10.0
            data["prices"] = prices

        gate = PublicationGate()
        report = gate.check(
            ValidationContext(
                as_of=run_date, prices=prices, prior_prices=data["prior_prices"],
                shares=data["shares"], corp_actions=data["corp_actions"],
                fx=data["fx"], prior_fx=data["prior_fx"], config=self.config,
            ),
            run_id=f"inputs-{run_date}",
        )
        blocking = [f for f in report.blocking if f.category in
                    {"schema", "range", "cross_source"}]
        if blocking:
            raise ValidationFailedError(
                "input validation failed: "
                + "; ".join(f"{f.rule} - {f.message}" for f in blocking)
            )
        return {"report": report, "prices": prices}

    def calculate_index(self, context: dict[str, Any]) -> dict[str, Any]:
        """Roll the index forward exactly one day from the persisted state.

        This is the part that makes a production index different from a backtest: it is
        incremental. Yesterday's divisor and constituent set are inputs, not something
        recomputed, so a corrupted state file has no cheap recovery.
        """
        run_date: dt.date = context["run_date"]
        data = context["load_market_data"]
        prices = context["validate_inputs"]["prices"]

        stored = IndexStateFile.load(self.state_dir, self.config.index_id)
        if stored is None:
            raise PipelineError(
                f"no stored state for {self.config.index_id}. A daily run resumes from "
                "the previous close; seed it with `miniftse seed-state` or rebuild the "
                "full history."
            )

        state = stored.to_state()
        engine = CorporateActionEngine(
            withholding_tax={str(k): v for k, v in self.config.withholding_tax.items()}
        )
        calculator = IndexCalculator(config=self.config, fx=self.fx, engine=engine)

        book = _PriceBook(prices)
        rolled = calculator._mark(state, run_date, book)

        events = parse_events(data["corp_actions"]) if not data["corp_actions"].empty \
            else []
        gross = net = 0.0
        if events:
            rolled, gross, net, _ = engine.apply_all(events, rolled)

        pr = rolled.level
        points = gross / rolled.divisor if rolled.divisor else 0.0
        net_points = net / rolled.divisor if rolled.divisor else 0.0
        gtr = stored.level_gtr * ((pr + points) / stored.level_pr) \
            if stored.level_pr else stored.level_gtr
        ntr = stored.level_ntr * ((pr + net_points) / stored.level_pr) \
            if stored.level_pr else stored.level_ntr

        return {
            "state": rolled, "price_return": pr, "gross_total_return": gtr,
            "net_total_return": ntr, "dividend_points": points,
            "prior": stored, "audit": engine.audit_frame(), "engine": engine,
        }

    def validate_output(self, context: dict[str, Any]) -> Any:
        run_date: dt.date = context["run_date"]
        data = context["load_market_data"]
        calc = context["calculate_index"]
        state: IndexState = calc["state"]

        gate = PublicationGate()
        return gate.check(
            ValidationContext(
                as_of=run_date,
                prices=context["validate_inputs"]["prices"],
                prior_prices=data["prior_prices"], shares=data["shares"],
                weights=pd.Series(state.weights()),
                constituents=dict.fromkeys(state.constituents, None),
                index_level=calc["price_return"],
                prior_index_level=calc["prior"].level_pr,
                divisor=state.divisor, prior_divisor=calc["prior"].divisor,
                total_market_value=state.total_market_value,
                divisor_audit=calc["audit"],
                # The FULL corporate action file, not the one the calculation saw. If a
                # row went missing between them, this is the check that finds it.
                corp_actions=data["all_corp_actions"],
                fx=data["fx"], prior_fx=data["prior_fx"], config=self.config,
            ),
            run_id=f"output-{run_date}",
        )

    def publication_gate(self, context: dict[str, Any]) -> str:
        report = context["validate_output"]
        allowed, message = PublicationGate().decide(report)
        if not allowed:
            raise ValidationFailedError(message)
        return message

    def publish(self, context: dict[str, Any]) -> dict[str, Any]:
        run_date: dt.date = context["run_date"]
        calc = context["calculate_index"]
        state: IndexState = calc["state"]

        state_file = IndexStateFile.from_state(
            self.config.index_id, state, calc["price_return"],
            calc["gross_total_return"], calc["net_total_return"],
        )
        path = state_file.save(self.state_dir)

        manifest = RunManifest.start(self.config.index_id, run_date, self.config)
        manifest.record_input("prices", context["validate_inputs"]["prices"])
        manifest.record_input("corp_actions", context["load_market_data"]["corp_actions"])
        manifest.record_output("state", path)
        manifest.record_metric("price_return", calc["price_return"])
        manifest.record_metric("gross_total_return", calc["gross_total_return"])
        manifest.record_metric("net_total_return", calc["net_total_return"])
        manifest.record_metric("divisor", state.divisor)
        manifest.record_metric("n_constituents", state.n_constituents)
        manifest.finish("success")
        ManifestStore(self.manifest_dir).save(manifest)

        return {"state_path": str(path), "run_id": manifest.run_id,
                "levels": {"pr": calc["price_return"],
                           "gtr": calc["gross_total_return"],
                           "ntr": calc["net_total_return"]}}

    def notify(self, context: dict[str, Any]) -> str:
        published = context["publish"]
        return (
            f"{self.config.index_id} published for {context['run_date']}: "
            f"PR {published['levels']['pr']:,.2f}, "
            f"GTR {published['levels']['gtr']:,.2f} "
            f"({published['run_id']})"
        )

    # ------------------------------------------------------------------ assembly

    def pipeline(self) -> Pipeline:
        return build_daily_pipeline(
            load_market_data=self.load_market_data,
            validate_inputs=self.validate_inputs,
            calculate_index=self.calculate_index,
            validate_output=self.validate_output,
            publication_gate=self.publication_gate,
            publish=self.publish,
            notify=self.notify,
            on_failure=self._on_failure,
        )

    def run(self, run_date: dt.date) -> PipelineRun:
        self._attempts = 0
        return self.pipeline().run(run_date)

    @staticmethod
    def _on_failure(result: StepResult, run: PipelineRun) -> None:
        print(f"  ALERT: step '{result.name}' failed for {run.run_date}: {result.error}")
        print("  Runbook: docs/RUNBOOK.md")

    # ------------------------------------------------------------------ seeding

    def seed_state(self, up_to: dt.date, verbose: bool = True) -> Path:
        """Build the history once and persist the closing state.

        A daily job needs a starting point. In production this is the previous day's
        published state; here it is produced by running the full historical build,
        which is also the disaster-recovery procedure.
        """
        from miniftse.production.build import BuildSpec, build_index

        spec = BuildSpec(index_config=self.config,
                         universe_config=self.universe_config,
                         start=self.config.base_date, end=up_to)
        result = build_index(spec, verbose=verbose)
        levels = result.history.levels.iloc[-1]

        # Persist the calculator's ACTUAL closing state, not a set re-derived from the
        # last review's specs. The calculator drops securities that have stopped
        # trading; re-deriving reinstates them at a carried price, and the next morning
        # the `constituents_priced` check blocks publication on a state the previous
        # run had already cleaned up.
        state = result.calculator.final_state
        if state is None:
            raise PipelineError("the build produced no final state to seed from")
        file = IndexStateFile.from_state(
            self.config.index_id, state, float(levels["price_return"]),
            float(levels["gross_total_return"]), float(levels["net_total_return"]),
        )
        return file.save(self.state_dir)
