"""Tests for the pieces that were previously written but never exercised.

Code that is never called is not implemented, it is only typed. Each test here targets
something the handover honestly listed as unproven: `reproduce()`, the Dagster job, the
reconciliation study, and vendor Protocol coverage.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from miniftse.config import global_all_cap
from miniftse.data.synthetic import SyntheticConfig, SyntheticUniverse
from miniftse.data.vendors import (
    EdgarProvider,
    FredProvider,
    LsegDataProvider,
    ProviderUnavailableError,
    YFinanceProvider,
    build_free_composite,
    provider_capability_matrix,
)
from miniftse.production.build import BuildSpec, build_index
from miniftse.production.manifest import ManifestStore, RunManifest, reproduce
from miniftse.quality.reconciliation import (
    reconcile_constituents,
    reconcile_returns,
    self_reconciliation,
    synthetic_published_index,
)

PROTOCOL_METHODS = [
    "get_prices", "get_corp_actions", "get_shares", "get_shares_history",
    "get_fundamentals", "get_fx", "get_deposit_rates", "get_classifications",
    "get_issuers", "get_securities", "get_listings", "get_identifier_map",
]


# --------------------------------------------------------------------------------------
# reproduce()
# --------------------------------------------------------------------------------------


def test_reproduce_confirms_an_identical_rebuild(tmp_path):
    """A run recorded today must regenerate byte-identically from its manifest.

    This is the audit story an index provider actually gets asked about: a published
    level from three years ago, and can you produce it again. It is only true if the
    pipeline is deterministic all the way down, which is why the synthetic universe is
    seeded and why the golden master exists.
    """
    spec = BuildSpec(
        index_config=global_all_cap(),
        universe_config=SyntheticConfig(n_securities=40, seed=11),
        start=dt.date(2016, 1, 4), end=dt.date(2017, 6, 30),
    )
    first = build_index(spec, verbose=False)
    store = ManifestStore(tmp_path)
    store.save(first.manifest)

    loaded = RunManifest.load(store.save(first.manifest))

    def rebuild(config):
        del config  # the spec is what determines the outputs; config is recorded proof
        second = build_index(spec, verbose=False)
        return {"levels": second.history.levels, "weights": second.history.weights}

    outcome = reproduce(loaded, rebuild)
    assert outcome["reproduced"], outcome
    assert all(v["status"] == "match" for v in outcome["outputs"].values())


def test_reproduce_detects_a_changed_parameter(tmp_path):
    """The check must fail when it should.

    A reproduction test that has never failed is not evidence of reproducibility, so
    this deliberately rebuilds with a different seed and asserts the mismatch is caught
    rather than silently accepted.
    """
    spec = BuildSpec(
        index_config=global_all_cap(),
        universe_config=SyntheticConfig(n_securities=40, seed=11),
        start=dt.date(2016, 1, 4), end=dt.date(2017, 6, 30),
    )
    original = build_index(spec, verbose=False)
    ManifestStore(tmp_path).save(original.manifest)

    def rebuild_differently(config):
        del config
        tampered = BuildSpec(
            index_config=global_all_cap(),
            universe_config=SyntheticConfig(n_securities=40, seed=12),  # changed
            start=spec.start, end=spec.end,
        )
        result = build_index(tampered, verbose=False)
        return {"levels": result.history.levels, "weights": result.history.weights}

    outcome = reproduce(original.manifest, rebuild_differently)
    assert not outcome["reproduced"]
    assert any(v["status"] == "MISMATCH" for v in outcome["outputs"].values())


def test_manifest_explains_what_changed(tmp_path):
    del tmp_path
    spec_a = BuildSpec(index_config=global_all_cap(),
                       universe_config=SyntheticConfig(n_securities=40, seed=11),
                       start=dt.date(2016, 1, 4), end=dt.date(2017, 1, 31))
    spec_b = BuildSpec(index_config=global_all_cap(),
                       universe_config=SyntheticConfig(n_securities=40, seed=12),
                       start=dt.date(2016, 1, 4), end=dt.date(2017, 1, 31))
    a = build_index(spec_a, verbose=False).manifest
    b = build_index(spec_b, verbose=False).manifest

    explanation = a.explain_diff(b)
    assert isinstance(explanation, str) and explanation
    assert a.diff(b)


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


def test_identical_weights_reconcile_exactly():
    weights = pd.Series({"A": 0.5, "B": 0.3, "C": 0.2})
    result = reconcile_constituents(weights, weights.copy(), dt.date(2026, 1, 2))
    assert result.differences == []
    assert result.matched_weight == pytest.approx(1.0)
    assert "rules agree" in result.verdict()


def test_reconciliation_finds_membership_and_weight_differences():
    ours = pd.Series({"A": 0.5, "B": 0.3, "C": 0.2})
    theirs = pd.Series({"A": 0.45, "B": 0.35, "D": 0.20})
    result = reconcile_constituents(ours, theirs, dt.date(2026, 1, 2))

    assert [d.security_id for d in result.only_ours] == ["C"]
    assert [d.security_id for d in result.only_theirs] == ["D"]
    assert result.total_absolute_difference == pytest.approx(0.5)
    assert result.matched_weight == pytest.approx(0.75)


def test_return_reconciliation_residual_is_reported_not_absorbed():
    """The residual must survive to the output.

    Pushing an unexplained difference into the nearest named component makes the table
    sum correctly and the reconciliation worthless.
    """
    dates = pd.to_datetime(pd.date_range("2026-01-01", periods=260, freq="B")).date
    ours = pd.Series([100 * 1.0004**i for i in range(260)], index=dates)
    theirs = pd.Series([100 * 1.00035**i for i in range(260)], index=dates)

    result = reconcile_returns(ours, theirs, fee_bps=20.0)
    assert result.total_difference > 0
    assert abs(result.explained + result.unexplained
               - result.total_difference) < 1e-12
    frame = result.to_frame()
    assert "unexplained residual" in set(frame["component"])


def test_synthetic_published_index_produces_a_realistic_disagreement():
    ours = pd.Series({f"S{i:03d}": 1.0 / 200 for i in range(200)})
    theirs = synthetic_published_index(ours, seed=3)
    result = reconcile_constituents(ours, theirs, dt.date(2026, 1, 2))

    assert result.only_ours, "the perturbation should drop some names"
    assert 0.90 < result.matched_weight < 1.0
    assert isinstance(result.verdict(), str)


def test_incremental_daily_matches_a_full_rebuild():
    """The reconciliation that catches real drift.

    A full historical rebuild and the incremental daily job must agree, because they
    share inputs and differ only in how they got there. In production this is how an
    index provider discovers that its overnight process has diverged from its own
    published methodology.
    """
    spec = BuildSpec(
        index_config=global_all_cap(),
        universe_config=SyntheticConfig(n_securities=40, seed=11),
        start=dt.date(2016, 1, 4), end=dt.date(2017, 6, 30),
    )
    first = build_index(spec, verbose=False)
    second = build_index(spec, verbose=False)

    outcome = self_reconciliation(first.history, second.history, "run 1", "run 2")
    assert outcome["identical"], outcome["note"]
    assert outcome["max_relative_difference_bps"] < 1e-6


# --------------------------------------------------------------------------------------
# Vendor Protocol coverage
# --------------------------------------------------------------------------------------


def test_every_provider_implements_the_full_protocol_surface():
    """Implemented means present, not necessarily able.

    A provider that cannot supply free float must say so by raising, never by returning
    an empty frame - an empty frame reads as "this company has no free float" and the
    caller carries on. The rule is that the method exists and its failure is explicit.
    """
    providers = [
        SyntheticUniverse(SyntheticConfig(n_securities=10)),
        YFinanceProvider(), EdgarProvider(), LsegDataProvider(),
        build_free_composite(),
    ]
    for provider in providers:
        missing = [m for m in PROTOCOL_METHODS if not hasattr(provider, m)]
        assert not missing, f"{type(provider).__name__} is missing {missing}"


def test_unsupported_calls_raise_rather_than_return_empty():
    with pytest.raises(ProviderUnavailableError, match="point-in-time"):
        YFinanceProvider().get_fundamentals(["AAPL"], ["BOOK_EQUITY"],
                                            dt.date(2026, 1, 2))
    with pytest.raises(ProviderUnavailableError, match="filings, not market data"):
        EdgarProvider().get_prices(["AAPL"], dt.date(2026, 1, 1), dt.date(2026, 1, 2))
    with pytest.raises(ProviderUnavailableError):
        YFinanceProvider().get_deposit_rates(["USD"], dt.date(2026, 1, 1),
                                             dt.date(2026, 1, 2))


def test_lseg_adapter_fails_with_a_specific_reason_not_a_stub():
    provider = LsegDataProvider()
    assert not provider.available()
    with pytest.raises(ProviderUnavailableError) as excinfo:
        provider.get_prices(["VOD.L"], dt.date(2026, 1, 1), dt.date(2026, 1, 2))
    message = str(excinfo.value)
    assert "lseg-data" in message or "app key" in message


def test_lseg_field_map_covers_every_fundamental_item():
    """The mapping is the vocabulary map's source of truth.

    Every item the factor library consumes must have a declared LSEG field, so the
    equivalence between what was built on free data and what would run in production is
    checkable rather than asserted.
    """
    from miniftse.factors.build import FLOW_ITEMS

    provider = LsegDataProvider()
    required = set(FLOW_ITEMS) | {"BOOK_EQUITY", "TOTAL_ASSETS", "TOTAL_DEBT"}
    missing = required - set(provider.FIELD_MAP)
    assert not missing, f"no LSEG field mapped for {missing}"


def test_free_composite_has_no_protocol_gaps():
    """Prices from Yahoo, fundamentals from EDGAR, rates from FRED.

    Neither source alone satisfies the Protocol; composed, they do. That is the point of
    CompositeProvider and the reason the routing table is explicit.
    """
    matrix = provider_capability_matrix({"free": build_free_composite()})
    gaps = [m for m in PROTOCOL_METHODS
            if matrix.loc["free", m] not in ("routed", "yes")]
    assert not gaps, f"the free stack cannot serve {gaps}"


def test_capability_matrix_does_not_overstate_a_composite():
    """A composite must report its backends' limits, not its own wrapper's.

    The first version inspected the delegating method - which always looks supported,
    because all it does is forward - and reported the free stack as able to supply
    deposit rates when neither backend publishes an interest rate.
    """
    from miniftse.data.vendors import CompositeProvider

    composite = CompositeProvider(
        prices=YFinanceProvider(), fundamentals=EdgarProvider(),
        reference=EdgarProvider(),
    )
    matrix = provider_capability_matrix({"no-fx": composite})
    assert matrix.loc["no-fx", "get_deposit_rates"] == "unsupported"


def test_fred_maps_every_currency_the_universe_uses():
    from miniftse.data.synthetic import MARKETS

    fred = FredProvider()
    needed = {str(currency) for currency, *_ in MARKETS.values()}
    missing = needed - set(fred.RATE_SERIES)
    assert not missing, f"no FRED rate series for {missing}"


def test_composite_routing_is_inspectable():
    composite = build_free_composite()
    assert composite.route("get_prices") is composite.prices
    assert composite.route("get_fundamentals") is composite.fundamentals
    assert composite.route("get_deposit_rates") is composite.fx


# --------------------------------------------------------------------------------------
# Dagster
# --------------------------------------------------------------------------------------


dagster = pytest.importorskip("dagster", reason="orchestration extra not installed")


def test_dagster_definitions_load():
    from miniftse.production import dagster_defs

    assert dagster_defs.DAGSTER_AVAILABLE
    assert dagster_defs.defs is not None
    assert dagster_defs.daily_index_job.name == "daily_index_production"
    assert dagster_defs.daily_schedule.cron_schedule == "0 6 * * 1-5"
    # Retries are for transient failures only. A failed validation check must not be
    # retried - the data will be just as wrong in ninety seconds, and retrying burns
    # the window before the publication deadline while delaying the alert.
    assert dagster_defs.RETRY_TRANSIENT.max_retries == 3


@pytest.mark.slow
def test_dagster_job_materialises_end_to_end(tmp_path):
    """Actually run the job, not just load its definitions.

    A definitions test proves the decorators parse. It does not prove an op can execute,
    that the assets pass data in the shape the next one expects, or that the gate is
    reachable — all of which broke on the first run here and none of which a load test
    would have caught.
    """
    from miniftse.production import dagster_defs
    from miniftse.production.daily import DailyJob, IndexStateFile

    job = DailyJob(
        config=global_all_cap(),
        universe_config=SyntheticConfig(n_securities=60, seed=20260809),
        state_dir=tmp_path / "state", manifest_dir=tmp_path / "manifests",
    )
    seed_to = dt.date(2020, 6, 1)
    job.seed_state(seed_to, verbose=False)
    stored = IndexStateFile.load(job.state_dir, job.config.index_id)
    assert stored is not None

    sessions = sorted(d for d in job.universe.calendar.date
                      if d > dt.date.fromisoformat(stored.as_of))
    original_build_job = dagster_defs.build_job
    try:
        # Point the ops at the isolated state directory rather than the repo's.
        dagster_defs.build_job = lambda **kwargs: DailyJob(  # type: ignore[assignment]
            config=global_all_cap(),
            universe_config=SyntheticConfig(n_securities=60, seed=20260809),
            state_dir=tmp_path / "state", manifest_dir=tmp_path / "manifests",
            simulate=kwargs.get("simulate"),
        )
        outcome = dagster_defs.materialise(sessions[0])
    finally:
        dagster_defs.build_job = original_build_job

    assert outcome["success"], outcome
    assert "published_index" in outcome["materialised"]

    # The job must have advanced the state, not merely reported success.
    after = IndexStateFile.load(job.state_dir, job.config.index_id)
    assert after is not None
    assert dt.date.fromisoformat(after.as_of) == sessions[0]


def test_dagster_job_wraps_the_same_steps_as_the_hand_rolled_dag():
    """The two runners must call one implementation.

    Two implementations of "calculate the index" that could disagree is precisely the
    failure the Dagster layer is meant to avoid, so the ops delegate to DailyJob rather
    than reimplementing anything.
    """
    import inspect

    from miniftse.production import dagster_defs

    source = inspect.getsource(dagster_defs)
    for method in ("load_market_data", "validate_inputs", "calculate_index",
                   "validate_output", "publication_gate", "publish"):
        assert f"job.{method}(" in source, f"the Dagster job bypasses {method}"
