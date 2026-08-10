"""Integration and golden-master tests.

Slower than the unit tests and worth every second. These are the ones that would catch
a refactor changing the published index, which is the failure mode that actually costs
money at an index provider.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from miniftse.calc.state import Constituent, IndexState
from miniftse.data.store import SQL_PATTERNS, PitStore
from miniftse.data.synthetic import SyntheticConfig, SyntheticUniverse
from miniftse.production.build import BuildSpec, build_index
from miniftse.production.daily import IndexStateFile
from miniftse.production.golden import GoldenMaster, compare, introduce_regression

GOLDEN_DIR = Path(__file__).parent / "golden"

# Small and short so the suite stays runnable on every commit. The pinned master in
# `pin-golden` uses a larger universe; this one exists to prove the mechanism works.
SMALL = BuildSpec(
    universe_config=SyntheticConfig(n_securities=80, seed=20260809),
    start=dt.date(2016, 1, 4),
    end=dt.date(2018, 12, 31),
)


@pytest.fixture(scope="module")
def universe() -> SyntheticUniverse:
    return SyntheticUniverse(SyntheticConfig(n_securities=60, seed=20260809))


@pytest.fixture(scope="module")
def built():  # type: ignore[no-untyped-def]
    return build_index(SMALL, verbose=False)


class TestDeterminism:
    def test_same_seed_gives_the_same_universe(self) -> None:
        a = SyntheticUniverse(SyntheticConfig(n_securities=40, seed=7))
        b = SyntheticUniverse(SyntheticConfig(n_securities=40, seed=7))
        assert a.summary() == b.summary()
        assert a._generated["prices"]["close"].sum() == pytest.approx(
            b._generated["prices"]["close"].sum())

    def test_different_seeds_give_different_universes(self) -> None:
        a = SyntheticUniverse(SyntheticConfig(n_securities=40, seed=7))
        b = SyntheticUniverse(SyntheticConfig(n_securities=40, seed=8))
        assert a.config.fingerprint() != b.config.fingerprint()

    def test_two_builds_agree_exactly(self) -> None:
        """The precondition for a golden master. If this fails, nothing downstream of
        it means anything."""
        one = build_index(SMALL, verbose=False)
        two = build_index(SMALL, verbose=False)
        assert one.history.levels["gross_total_return"].iloc[-1] == pytest.approx(
            two.history.levels["gross_total_return"].iloc[-1], rel=1e-12)


class TestIndexIntegrity:
    def test_divisor_continuity_holds_across_every_event(self, built) -> None:  # type: ignore[no-untyped-def]
        breaches = built.calculator.engine.continuity_breaches(tolerance_bps=1.0)
        assert breaches == [], (
            f"{len(breaches)} divisor events moved the index level when they should "
            f"not have: {[(b.event_type, b.security_id) for b in breaches[:5]]}"
        )

    def test_index_never_empties(self, built) -> None:  # type: ignore[no-untyped-def]
        assert built.history.levels["n_constituents"].min() > 0

    def test_levels_are_positive_and_finite(self, built) -> None:  # type: ignore[no-untyped-def]
        levels = built.history.levels
        for column in ("price_return", "gross_total_return", "net_total_return",
                       "divisor"):
            assert (levels[column] > 0).all()
            assert levels[column].notna().all()

    def test_total_return_beats_price_return(self, built) -> None:  # type: ignore[no-untyped-def]
        """Over a multi-year period with dividends, GTR must exceed PR."""
        levels = built.history.levels
        assert levels["gross_total_return"].iloc[-1] > levels["price_return"].iloc[-1]

    def test_net_sits_between_price_and_gross(self, built) -> None:  # type: ignore[no-untyped-def]
        levels = built.history.levels.iloc[-1]
        assert (levels["price_return"] <= levels["net_total_return"]
                <= levels["gross_total_return"])

    def test_income_yield_is_plausible(self, built) -> None:  # type: ignore[no-untyped-def]
        """GTR minus PR over a full year should look like a dividend yield.

        A cheap check that catches double-counted or missing dividends, which are
        otherwise invisible until someone reconciles against a published series.
        """
        from miniftse.calc.index import annual_income_check

        income = annual_income_check(built.history)
        full_years = income.iloc[:-1] if len(income) > 1 else income
        assert full_years["plausible"].all(), (
            f"implausible income yields: "
            f"{full_years.loc[~full_years['plausible'], 'income_yield'].to_dict()}"
        )

    def test_review_turnover_is_bounded(self, built) -> None:  # type: ignore[no-untyped-def]
        reviews = built.history.reviews
        if reviews.empty:
            pytest.skip("no reviews in this window")
        assert (reviews["one_way_turnover"] >= 0).all()
        assert (reviews["one_way_turnover"] <= 1.0).all()

    def test_reviews_are_level_continuous(self, built) -> None:  # type: ignore[no-untyped-def]
        reviews = built.history.reviews
        if reviews.empty:
            pytest.skip("no reviews in this window")
        assert reviews["level_continuity_bps"].abs().max() < 0.01

    def test_review_populates_constituent_adv(self):
        from miniftse.calc.fx import FxTable
        from miniftse.config import global_all_cap
        from miniftse.review.reconstitution import ReconstitutionEngine

        universe = SyntheticUniverse(SyntheticConfig(n_securities=60, seed=20260809))
        prices = universe._generated["prices"]
        shares = universe._generated["shares"]
        securities = universe.get_securities()
        config = global_all_cap()

        quotes = list(universe._fx["quote"].unique())
        fx = FxTable.from_frame(
            universe.get_fx("USD", quotes, universe.config.start, universe.config.end),
            universe.get_deposit_rates(quotes, universe.config.start, universe.config.end),
            base=str(config.base_currency),
        )
        spot = {c: fx.rate(config.base_date, c) for c in fx.currencies()}

        reconstitution = ReconstitutionEngine(
            config=config, prices=prices, shares=shares, securities=securities,
            fx_rates=spot,
        )
        # No effective_dates() call first -> constituents_for falls back to screening at
        # the date itself (reconstitution.py:186-189), which is exactly what's needed
        # here: run one review, check what it produced.
        specs = reconstitution.constituents_for(config.base_date)
        assert specs, "review produced no constituents - check the fixture"
        assert any(spec.adv > 0.0 for spec in specs.values())


class TestGoldenMaster:
    def test_pin_and_match(self, built, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        master = GoldenMaster.create("test", built.history.levels)
        master.save(tmp_path)
        reloaded = GoldenMaster.load(tmp_path, "test")
        result = compare(reloaded, built.history.levels)
        assert result.passed, result.report()
        assert result.max_diff_bps < 1e-6

    def test_catches_a_half_basis_point_drift(self, built) -> None:  # type: ignore[no-untyped-def]
        """Deliberately introduce a regression and confirm the master catches it.

        A regression test nobody has watched fail is of unknown value. Half a basis
        point is far too small to notice by eye and far too large to publish.
        """
        master = GoldenMaster.create("test", built.history.levels)
        drifted = introduce_regression(built.history.levels, bps=0.5, from_index=200)
        result = compare(master, drifted)
        assert not result.passed
        assert result.max_diff_bps == pytest.approx(0.5, rel=0.05)
        assert result.first_divergence is not None

    def test_reference_master_if_pinned(self) -> None:
        """Compare against the committed reference master, if one exists.

        Skipped rather than failed when absent, so a fresh clone is not blocked before
        `miniftse pin-golden` has been run.
        """
        meta = GOLDEN_DIR / "reference.json"
        if not meta.exists():
            pytest.skip("no reference master pinned; run `miniftse pin-golden`")
        master = GoldenMaster.load(GOLDEN_DIR, "reference")
        spec = BuildSpec(
            universe_config=SyntheticConfig(n_securities=300, seed=20260809),
            start=dt.date(2016, 1, 4), end=dt.date(2024, 12, 31),
        )
        result = compare(master, build_index(spec, verbose=False).history.levels)
        assert result.passed, result.report()


class TestPointInTime:
    def test_pit_query_never_returns_future_filings(self, universe) -> None:  # type: ignore[no-untyped-def]
        as_of = dt.date(2020, 6, 30)
        frame = universe.get_fundamentals(None, ["BOOK_EQUITY", "NET_INCOME"], as_of)
        assert (frame["filed_date"] <= as_of).all()

    def test_naive_query_does_leak(self, universe) -> None:  # type: ignore[no-untyped-def]
        """The look-ahead guard must be shown to be doing work.

        A guard that has never been observed to make a difference is not evidence of
        anything. This asserts the naive query and the PIT query genuinely disagree.
        """
        store = PitStore()
        store.load_universe(universe)
        as_of = dt.date(2020, 6, 30)
        import pandas as pd

        pit = store.pit_fundamentals(as_of, ["BOOK_EQUITY"])
        naive = store.naive_fundamentals(as_of, ["BOOK_EQUITY"])
        # DuckDB returns DATE columns as datetime64, so compare like with like.
        assert pd.Timestamp(naive["filed_date"].max()) > pd.Timestamp(as_of)
        merged = pit.merge(naive, on=["security_id", "item"], suffixes=("_p", "_n"))
        differing = (merged["value_p"] - merged["value_n"]).abs() > 1e-6
        assert differing.any(), "PIT and naive queries agree, so the guard is untested"
        store.close()

    def test_all_sql_patterns_parse(self, universe) -> None:  # type: ignore[no-untyped-def]
        """Every documented query must actually run. A cookbook of broken SQL is worse
        than none, because people copy from it."""
        store = PitStore()
        store.load_universe(universe)
        import pandas as pd

        # `to_date` must be typed as a date even though it is all-NULL. A column of
        # Nones arrives as INTEGER and DuckDB refuses to compare it to a date - which
        # is the correct behaviour and exactly the open-interval case the query is for.
        store.register("index_membership", pd.DataFrame({
            "index_id": ["X"], "security_id": ["SEC00000"], "weight": [1.0],
            "from_date": pd.to_datetime([dt.date(2020, 1, 1)]),
            "to_date": pd.Series([None], dtype="datetime64[ns]"),
        }))
        store.register("membership_daily", pd.DataFrame({
            "index_id": ["X"], "security_id": ["SEC00000"],
            "as_of_date": pd.to_datetime([dt.date(2020, 1, 1)]),
        }))
        store.register("signals", pd.DataFrame({
            "as_of": pd.to_datetime([dt.date(2020, 1, 1)]),
            "security_id": ["SEC00000"], "signal": [0.5],
        }))

        params = {
            "pit_fundamental": {"as_of": dt.date(2020, 6, 30), "items": ["BOOK_EQUITY"]},
            "ttm_fundamental": {"as_of": dt.date(2020, 6, 30), "item": "NET_INCOME"},
            "membership_as_of": {"as_of": dt.date(2020, 6, 30)},
            "turnover_between_reviews": {"index_id": "X", "d0": dt.date(2020, 1, 1),
                                         "d1": dt.date(2020, 6, 30)},
            "identifier_as_of": {"id_type": "sedol", "id_value": "0000006",
                                 "as_of": dt.date(2020, 6, 30)},
            "stale_price_detection": {"min_run": 3},
        }
        for name, sql in SQL_PATTERNS.items():
            store.sql(sql, **params.get(name, {}))
        store.close()


class TestValidation:
    def test_clean_build_has_no_escalating_findings(self, built) -> None:  # type: ignore[no-untyped-def]
        assert built.validation is not None
        assert built.validation.escalating == [], (
            f"escalating findings on clean data: "
            f"{[f.rule for f in built.validation.escalating]}"
        )

    def test_gate_blocks_when_a_blocking_check_fails(self) -> None:
        from miniftse.quality.rules import (
            PublicationGate,
            ValidationContext,
            ValidationEngine,
        )

        engine = ValidationEngine.default()
        gate = PublicationGate(engine)
        # An empty context fails the schema checks, which are blocking.
        report = gate.check(ValidationContext(as_of=dt.date(2024, 1, 1)), "test")
        allowed, message = gate.decide(report)
        assert not allowed
        assert "BLOCK" in message or "ESCALATE" in message

    def test_chaos_drill_detects_every_injected_fault(self, built) -> None:  # type: ignore[no-untyped-def]
        import pandas as pd

        from miniftse.quality.faults import build_baseline_context, run_chaos_drill

        universe = built.universe
        prices = universe._generated["prices"]
        as_of = built.history.levels.iloc[-1]["date"]
        prior_dates = sorted(d for d in prices["date"].unique() if d < as_of)
        snapshot = built.history.weights
        weights = snapshot[snapshot["date"] == snapshot["date"].max()].set_index(
            "security_id")["weight"]
        quotes = list(universe._fx["quote"].unique())

        context = build_baseline_context(
            prices=prices[prices["date"] == as_of],
            prior_prices=prices[prices["date"] == prior_dates[-1]],
            weights=weights, shares=universe.get_shares(None, as_of),
            fx=universe.get_fx("USD", quotes, as_of, as_of),
            prior_fx=universe.get_fx("USD", quotes, prior_dates[-1], prior_dates[-1]),
            as_of=as_of,
            divisor=float(built.history.levels.iloc[-1]["divisor"]),
            index_level=float(built.history.levels.iloc[-1]["price_return"]),
            total_market_value=float(
                built.history.levels.iloc[-1]["total_market_value"]),
            divisor_audit=built.calculator.engine.audit_frame(),
            corp_actions=universe.get_corp_actions(None, as_of, as_of),
            config=built.history.config,
            prior_index_level=float(built.history.levels.iloc[-2]["price_return"]),
            prior_divisor=float(built.history.levels.iloc[-2]["divisor"]),
        )
        frame, _ = run_chaos_drill(context)
        injected = frame[~frame["detail"].str.startswith("NOT INJECTED")]
        undetected = injected[~injected["detected"]]
        assert undetected.empty, (
            f"undetected faults: {undetected['fault_name'].tolist()}"
        )
        assert isinstance(frame, pd.DataFrame)


class TestManifest:
    def test_manifest_records_inputs_and_outputs(self, built) -> None:  # type: ignore[no-untyped-def]
        manifest = built.manifest
        assert manifest.input_hashes
        assert manifest.output_hashes
        assert manifest.config_hash
        assert manifest.status in {"success", "blocked"}

    def test_identical_runs_produce_identical_hashes(self) -> None:
        one = build_index(SMALL, verbose=False).manifest
        two = build_index(SMALL, verbose=False).manifest
        assert one.output_hashes == two.output_hashes
        assert one.input_hashes == two.input_hashes

    def test_diff_reports_a_config_change(self) -> None:
        from dataclasses import replace

        base = build_index(SMALL, verbose=False).manifest
        changed_spec = replace(
            SMALL, index_config=replace(SMALL.index_config, base_level=500.0))
        changed = build_index(changed_spec, verbose=False).manifest
        diff = base.diff(changed)
        assert "config" in diff
        assert "base_level" in diff["config"]


class TestIndexStateFile:
    def test_state_file_round_trips_adv(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        state = IndexState(
            date=dt.date(2020, 1, 1), divisor=1.0,
            constituents={"S1": Constituent("S1", price=10.0, shares=100.0,
                                             adv=1_234_567.0)},
        )
        saved = IndexStateFile.from_state("MFTSE-TEST", state, pr=100.0, gtr=100.0,
                                           ntr=100.0)
        saved.save(tmp_path)
        loaded = IndexStateFile.load(tmp_path, "MFTSE-TEST")
        restored = loaded.to_state()
        assert restored.constituents["S1"].adv == 1_234_567.0

    def test_state_file_without_adv_key_loads_with_zero_default(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # Simulates a state file written before this field existed.
        import json
        path = tmp_path / "MFTSE-OLD_state.json"
        path.write_text(json.dumps({
            "index_id": "MFTSE-OLD", "as_of": "2020-01-01", "divisor": 1.0,
            "level_pr": 100.0, "level_gtr": 100.0, "level_ntr": 100.0,
            "constituents": {
                "S1": {
                    "price": 10.0, "shares": 100.0, "free_float_factor": 1.0,
                    "capping_factor": 1.0, "fx_rate": 1.0, "currency": "USD",
                    "country": "US", "icb_industry": "", "size_band": "LARGE",
                }
            },
        }), encoding="utf-8")
        loaded = IndexStateFile.load(tmp_path, "MFTSE-OLD")
        restored = loaded.to_state()
        assert restored.constituents["S1"].adv == 0.0


class TestAiLayer:
    def test_assistant_answers_with_citations(self) -> None:
        from miniftse.agents.rag import MethodologyAssistant

        root = Path(__file__).parent.parent / "ground_rules"
        assistant = MethodologyAssistant().add_directory(root)
        answer = assistant.ask("What is the minimum free float in a developed market?")
        assert not answer.abstained
        assert answer.citations
        assert "5%" in answer.answer

    def test_assistant_abstains_out_of_scope(self) -> None:
        from miniftse.agents.rag import MethodologyAssistant

        root = Path(__file__).parent.parent / "ground_rules"
        assistant = MethodologyAssistant().add_directory(root)
        for question in ("What is the index level today?",
                         "Should I buy this index?",
                         "What is the MSCI free float threshold?"):
            assert assistant.ask(question).abstained, question

    def test_eval_suite_meets_its_reported_bar(self) -> None:
        """Guards the number quoted in the AI proposal.

        If the headline says 85% and the suite scores 60%, the proposal is wrong. This
        test is what stops that happening silently.
        """
        from miniftse.agents.evals import default_eval_set, run_evals
        from miniftse.agents.rag import MethodologyAssistant

        root = Path(__file__).parent.parent / "ground_rules"
        report = run_evals(MethodologyAssistant().add_directory(root),
                           default_eval_set())
        assert report.accuracy >= 0.85, report.summary()
        assert report.citation_precision >= 0.95
        assert report.hallucination_rate == 0.0
        assert report.abstention_accuracy >= 0.95

    def test_number_guard_blocks_an_invented_figure(self) -> None:
        from miniftse.agents.drafter import FactPack, NumberGuard

        pack = FactPack()
        pack.add("index_return", 0.082, "index return", "IndexHistory")
        assert NumberGuard.check("The index returned 8.20%.", pack).passed
        result = NumberGuard.check("The index returned 8.20%, and fees cost 0.42%.",
                                   pack)
        assert not result.passed
        assert "0.42" in result.unverified_numbers
