"""Tests for the ops desk: context persistence, snapshot build, and the app."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pandas as pd
import pytest

from miniftse.config import global_all_cap
from miniftse.quality.rules import ValidationContext


def _context() -> ValidationContext:
    as_of = dt.date(2020, 6, 30)
    prices = pd.DataFrame({
        "security_id": ["S1", "S2"], "date": [as_of, as_of], "close": [10.0, 20.0],
    })
    return ValidationContext(
        as_of=as_of,
        prices=prices,
        prior_prices=prices.assign(close=[9.5, 20.5]),
        weights=pd.Series({"S1": 0.6, "S2": 0.4}),
        shares=pd.DataFrame({"security_id": ["S1", "S2"], "shares": [100.0, 50.0]}),
        divisor=1234.5,
        prior_divisor=1230.0,
        index_level=1000.0,
        prior_index_level=998.0,
        total_market_value=1_234_500.0,
        constituents={"S1": None, "S2": None},
    )


def test_validation_context_round_trips(tmp_path):
    ctx = _context()
    ctx.save(tmp_path / "baseline")
    back = ValidationContext.load(tmp_path / "baseline")

    assert back.as_of == ctx.as_of
    assert back.divisor == ctx.divisor
    assert back.prior_divisor == ctx.prior_divisor
    assert back.index_level == ctx.index_level
    assert back.total_market_value == ctx.total_market_value
    assert set(back.constituents) == set(ctx.constituents)
    pd.testing.assert_frame_equal(back.prices, ctx.prices)
    pd.testing.assert_series_equal(back.weights, ctx.weights)


def test_validation_context_round_trips_none_fields(tmp_path):
    """Absent frames must come back absent, not as empty DataFrames.

    An empty frame and a missing frame mean different things to the rules: several
    checks in `quality/checks.py` short-circuit to a pass when their input is None and
    would report a spurious failure against an empty frame.
    """
    ctx = _context()
    ctx.save(tmp_path / "b")
    back = ValidationContext.load(tmp_path / "b")
    assert back.corp_actions is None
    assert back.alternate_source is None
    assert back.fx is None


def test_loaded_context_passes_the_same_rules(tmp_path):
    """The real acceptance criterion: same findings before and after a round trip."""
    from miniftse.quality.rules import ValidationEngine

    ctx = _context()
    engine = ValidationEngine.default()
    before = engine.run(ctx, run_id="before").to_frame()
    ctx.save(tmp_path / "b")
    after = engine.run(ValidationContext.load(tmp_path / "b"), run_id="after").to_frame()

    pd.testing.assert_frame_equal(
        before.drop(columns=["run_id"], errors="ignore"),
        after.drop(columns=["run_id"], errors="ignore"),
    )


def test_validation_context_round_trips_remaining_frames_and_config(tmp_path):
    """The fields `_context()` leaves at their default: the frames it doesn't set, the
    `official_level` scalar, and `config` resolved by name through the constructors in
    `miniftse.config`.
    """
    as_of = dt.date(2020, 6, 30)
    ctx = _context()
    ctx.corp_actions = pd.DataFrame({"security_id": ["S1"], "action": ["SPLIT"]})
    ctx.divisor_audit = pd.DataFrame({"date": [as_of], "divisor": [1234.5]})
    ctx.reference = pd.DataFrame({"security_id": ["S1", "S2"], "country": ["US", "GB"]})
    ctx.alternate_source = pd.DataFrame({"security_id": ["S1"], "close": [10.1]})
    ctx.fx = pd.DataFrame({"base": ["USD"], "quote": ["GBP"], "rate": [0.8]})
    ctx.prior_fx = pd.DataFrame({"base": ["USD"], "quote": ["GBP"], "rate": [0.79]})
    ctx.official_level = 999.5
    ctx.config = global_all_cap()

    ctx.save(tmp_path / "full")
    back = ValidationContext.load(tmp_path / "full")

    pd.testing.assert_frame_equal(back.corp_actions, ctx.corp_actions)
    pd.testing.assert_frame_equal(back.divisor_audit, ctx.divisor_audit)
    pd.testing.assert_frame_equal(back.reference, ctx.reference)
    pd.testing.assert_frame_equal(back.alternate_source, ctx.alternate_source)
    pd.testing.assert_frame_equal(back.fx, ctx.fx)
    pd.testing.assert_frame_equal(back.prior_fx, ctx.prior_fx)
    assert back.official_level == ctx.official_level
    assert back.config == ctx.config


def test_validation_context_save_rejects_config_not_built_by_name(tmp_path):
    """`config` only round-trips as a name resolved through `miniftse.config`'s named
    constructors (`global_all_cap` and its siblings). A hand-built IndexConfig can't be
    identified by name, so save() must fail loudly rather than silently drop it.
    """
    ctx = _context()
    ctx.config = replace(global_all_cap(), index_id="CUSTOM")

    with pytest.raises(ValueError, match="not one of the named constructors"):
        ctx.save(tmp_path / "bad-config")


# --------------------------------------------------------------------------------------
# Task 2: the snapshot build
# --------------------------------------------------------------------------------------


@pytest.mark.slow
def test_build_snapshot_writes_every_expected_file(tmp_path):
    from miniftse.data.synthetic import SyntheticConfig
    from miniftse.desk.snapshot import EXPECTED_FILES, build_snapshot
    from miniftse.production.build import BuildSpec

    spec = BuildSpec(
        universe_config=SyntheticConfig(n_securities=100, seed=20260809),
        start=dt.date(2019, 1, 2), end=dt.date(2019, 12, 31),
    )
    build_snapshot(tmp_path, spec)
    for name in EXPECTED_FILES:
        assert (tmp_path / name).exists(), f"snapshot did not write {name}"


def test_build_snapshot_fails_loudly_on_missing_artefact(tmp_path, monkeypatch):
    """A partial snapshot must never be written."""
    from miniftse.desk import snapshot as snap
    monkeypatch.setattr(snap, "_read_onepagers", lambda *a, **k: (_ for _ in ()).throw(
        FileNotFoundError("risk_onepager.md")))
    with pytest.raises(FileNotFoundError, match="risk_onepager"):
        snap.build_snapshot(tmp_path)
    assert not (tmp_path / "manifest.json").exists()
