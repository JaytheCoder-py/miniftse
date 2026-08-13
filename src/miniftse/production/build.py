"""End-to-end index construction: the function `make build-index` calls.

Everything above this module is a component. This is the wiring, and it is deliberately
one readable function rather than a framework - the order in which an index is built is
domain knowledge, and burying it in configuration would hide the only part a new
colleague actually needs to read.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from miniftse.calc.fx import FxTable
from miniftse.calc.index import IndexCalculator, IndexHistory, annual_income_check
from miniftse.config import IndexConfig, global_all_cap
from miniftse.corpactions.engine import CorporateActionEngine
from miniftse.data.providers import UniverseData
from miniftse.data.synthetic import SyntheticConfig, SyntheticUniverse
from miniftse.production.manifest import RunManifest
from miniftse.quality.rules import PublicationGate, ValidationContext, ValidationReport
from miniftse.review.reconstitution import ReconstitutionEngine


@dataclass
class BuildResult:
    """Everything one build produced."""

    history: IndexHistory
    manifest: RunManifest
    validation: ValidationReport | None
    may_publish: bool
    gate_message: str
    universe: UniverseData
    reconstitution: ReconstitutionEngine
    calculator: IndexCalculator
    duration: float
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        out = dict(self.history.summary())
        out.update({
            "run_id": self.manifest.run_id,
            "git_sha": self.manifest.git_sha[:12],
            "may_publish": self.may_publish,
            "gate": self.gate_message,
            "build_seconds": round(self.duration, 1),
            "warnings": len(self.warnings),
        })
        return out


@dataclass
class BuildSpec:
    """Inputs to a build. Everything here goes into the run manifest."""

    index_config: IndexConfig = field(default_factory=global_all_cap)
    universe_config: SyntheticConfig = field(default_factory=SyntheticConfig)
    start: dt.date = dt.date(2016, 1, 4)
    end: dt.date = dt.date(2026, 6, 30)
    validate: bool = True
    manifest_dir: Path | None = None

    universe: UniverseData | None = None
    """The universe to build from. `None` generates a synthetic one from
    `universe_config`, which is what every existing caller wants and gets unchanged.

    Passing one instead is how a real-data build happens: `data.real` writes a snapshot,
    `MaterialisedUniverse` loads it, and the rest of this function cannot tell the
    difference. `universe_config` is ignored when this is set, and the manifest records
    the universe's own fingerprint rather than the config's."""


def build_index(spec: BuildSpec | None = None, verbose: bool = True) -> BuildResult:
    """Build one index history end to end.

    The order is not arbitrary:

    1. Generate or load the universe.
    2. Build the FX table, because the reconstitution needs base-currency market caps
       to rank securities and cannot screen without them.
    3. Construct the reconstitution engine - stateful, because buffers depend on the
       previous review.
    4. Run the daily calculation loop.
    5. Validate the final state and put it through the publication gate.
    """
    spec = spec or BuildSpec()
    started = time.perf_counter()
    log = print if verbose else (lambda *a, **k: None)

    manifest = RunManifest.start(
        index_id=spec.index_config.index_id, as_of=spec.end, config=spec.index_config
    )

    if spec.universe is None:
        log(f"[1/5] generating universe ({spec.universe_config.n_securities} securities)")
        universe: UniverseData = SyntheticUniverse(spec.universe_config)
        manifest.record_input("universe_config", {
            "seed": spec.universe_config.seed,
            "n_securities": spec.universe_config.n_securities,
            "fingerprint": spec.universe_config.fingerprint(),
        })
    else:
        universe = spec.universe
        log(f"[1/5] loading universe ({universe.name})")
        manifest.record_input("universe", {
            "name": universe.name,
            "fingerprint": universe.fingerprint,
            "start": universe.start.isoformat(),
            "end": universe.end.isoformat(),
        })

    prices = universe.prices
    shares = universe.shares
    corp_actions = universe.corp_actions
    securities = universe.get_securities()

    manifest.record_input("prices", prices)
    manifest.record_input("shares", shares)
    manifest.record_input("corp_actions", corp_actions)
    manifest.record_input("securities", securities)

    log("[2/5] building FX table")
    quotes = list(universe.fx_rates["quote"].unique())
    fx = FxTable.from_frame(
        universe.get_fx("USD", quotes, universe.start, universe.end),
        universe.get_deposit_rates(quotes, universe.start, universe.end),
        base=str(spec.index_config.base_currency),
    )
    # Spot rates as at the base date, used to put every security's market cap on a
    # common footing for the size screens. Using each review's own rates would make
    # size-band membership move with currencies, which is a real design question - and
    # a fixed reference makes the band boundaries reproducible.
    spot = {c: fx.rate(spec.start, c) for c in fx.currencies()}

    log("[3/5] constructing reconstitution engine")
    reconstitution = ReconstitutionEngine(
        config=spec.index_config, prices=prices, shares=shares,
        securities=securities, fx_rates=spot,
    )

    log(f"[4/5] running daily calculation {spec.start} to {spec.end}")
    engine = CorporateActionEngine(
        withholding_tax={str(k): v for k, v in spec.index_config.withholding_tax.items()}
    )
    calculator = IndexCalculator(config=spec.index_config, fx=fx, engine=engine)
    history = calculator.run(prices, corp_actions, reconstitution, spec.start, spec.end)

    manifest.record_output("levels", history.levels)
    manifest.record_output("weights", history.weights)
    for key, value in history.summary().items():
        manifest.record_metric(key, value)

    validation: ValidationReport | None = None
    may_publish, gate_message = True, "validation skipped"

    if spec.validate:
        log("[5/5] validating and running the publication gate")
        validation, may_publish, gate_message = _validate(
            history, prices, shares, universe, calculator, spec, fx
        )
        manifest.record_metric("validation_counts", validation.counts())
    else:
        log("[5/5] validation skipped")

    duration = time.perf_counter() - started
    manifest.finish("success" if may_publish else "blocked", duration)
    if spec.manifest_dir:
        manifest.save(spec.manifest_dir)

    log(f"      done in {duration:.1f}s - {gate_message}")
    return BuildResult(
        history=history, manifest=manifest, validation=validation,
        may_publish=may_publish, gate_message=gate_message, universe=universe,
        reconstitution=reconstitution, calculator=calculator, duration=duration,
        warnings=history.warnings,
    )


def _validate(
    history: IndexHistory,
    prices: pd.DataFrame,
    shares: pd.DataFrame,
    universe: UniverseData,
    calculator: IndexCalculator,
    spec: BuildSpec,
    fx: FxTable,
) -> tuple[ValidationReport, bool, str]:
    """Validate the final day of the history, as the daily job would."""
    levels = history.levels
    last = levels.iloc[-1]
    as_of = last["date"]
    prior = levels.iloc[-2] if len(levels) > 1 else last

    today_prices = prices[prices["date"] == as_of]
    prior_dates = sorted(d for d in prices["date"].unique() if d < as_of)
    prior_prices = (prices[prices["date"] == prior_dates[-1]]
                    if prior_dates else today_prices)

    final_weights = pd.Series(dtype=float)
    if not history.weights.empty:
        last_snapshot = history.weights[history.weights["date"] == as_of]
        if last_snapshot.empty:
            last_snapshot = history.weights[
                history.weights["date"] == history.weights["date"].max()
            ]
        final_weights = last_snapshot.set_index("security_id")["weight"]

    audit = calculator.engine.audit_frame()
    todays_audit = audit[audit["date"] == as_of] if not audit.empty else audit

    context = ValidationContext(
        as_of=as_of,
        prices=today_prices,
        prior_prices=prior_prices,
        shares=shares[shares["knowledge_date"] <= as_of]
        .sort_values(["security_id", "effective_date", "knowledge_date"])
        .groupby("security_id", as_index=False).last(),
        weights=final_weights,
        constituents=dict.fromkeys(final_weights.index, None),
        index_level=float(last["price_return"]),
        prior_index_level=float(prior["price_return"]),
        divisor=float(last["divisor"]),
        prior_divisor=float(prior["divisor"]),
        total_market_value=float(last["total_market_value"]),
        divisor_audit=todays_audit,
        corp_actions=universe.get_corp_actions(None, as_of, as_of),
        fx=universe.get_fx(str(spec.index_config.base_currency),
                           list(fx.currencies()), as_of, as_of),
        prior_fx=universe.get_fx(
            str(spec.index_config.base_currency), list(fx.currencies()),
            prior_dates[-1], prior_dates[-1]) if prior_dates else None,
        config=spec.index_config,
    )

    gate = PublicationGate()
    report = gate.check(context, run_id=f"build-{as_of}")
    may_publish, message = gate.decide(report)
    return report, may_publish, message


def build_and_report(spec: BuildSpec | None = None) -> BuildResult:
    """Build plus the standard console report."""
    result = build_index(spec)
    print()
    print("=" * 74)
    for key, value in result.summary().items():
        print(f"  {key:<24} {value}")
    print("=" * 74)

    if result.validation is not None:
        print()
        print(result.validation.summary())

    income = annual_income_check(result.history)
    if not income.empty:
        print()
        print("Annual income check (GTR - PR should look like a dividend yield):")
        print(income[["income_yield", "withholding_cost", "plausible"]].to_string())

    breaches = result.calculator.engine.continuity_breaches()
    print()
    print(f"Divisor continuity breaches: {len(breaches)}")
    return result
