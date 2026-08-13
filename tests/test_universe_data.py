"""The `UniverseData` seam: a materialised snapshot must be substitutable for the generator.

These tests exist because the seam was previously decorative. `data/providers.py` claimed
the engine binds to Protocols and never to a vendor, while `production/` reached into
`SyntheticUniverse._generated` - so a real provider could satisfy every documented method
and still not be substitutable. The regression to guard against is that coupling coming
back, and it comes back silently: a build keeps working, and only a *different* universe
implementation reveals it.

So the load-bearing test is `test_build_from_snapshot_matches_generator`. If someone
reintroduces a private-attribute read in the build path, a snapshot build stops matching a
generated one, and that test fails rather than the seam quietly rotting.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from miniftse.config import global_all_cap
from miniftse.data.materialised import MaterialisedUniverse, SnapshotError
from miniftse.data.providers import MarketDataProvider, UniverseData
from miniftse.data.synthetic import SyntheticConfig, SyntheticUniverse
from miniftse.production.build import BuildSpec, build_index

# The universe must start well before the build does: the eligibility screens need
# `liquidity_window_days` of history behind the first review, and a universe starting on
# the base date makes that review select nothing.
CONFIG = SyntheticConfig(
    n_securities=60, seed=20260809, start=dt.date(2015, 1, 1), end=dt.date(2017, 12, 29)
)
BUILD_START = dt.date(2016, 1, 4)
BUILD_END = dt.date(2017, 12, 29)


@pytest.fixture(scope="module")
def generated() -> SyntheticUniverse:
    return SyntheticUniverse(CONFIG)


@pytest.fixture(scope="module")
def snapshot(generated: SyntheticUniverse, tmp_path_factory: pytest.TempPathFactory
             ) -> MaterialisedUniverse:
    path = tmp_path_factory.mktemp("snapshot")
    generated.materialise(path)
    return MaterialisedUniverse(path)


# --------------------------------------------------------------------------------- shape


def test_both_satisfy_the_protocols(generated: SyntheticUniverse,
                                    snapshot: MaterialisedUniverse) -> None:
    for universe in (generated, snapshot):
        assert isinstance(universe, MarketDataProvider)
        assert isinstance(universe, UniverseData)


def test_generator_exposes_tables_without_private_access(
        generated: SyntheticUniverse) -> None:
    """The five accessors that replaced `_generated[...]` must be non-empty."""
    for name in ("prices", "shares", "corp_actions", "fundamentals", "fx_rates"):
        table = getattr(generated, name)
        assert isinstance(table, pd.DataFrame), name
        assert not table.empty, name


@pytest.mark.parametrize(
    "table", ["prices", "shares", "corp_actions", "fundamentals", "fx_rates"]
)
def test_snapshot_round_trips_every_table(generated: SyntheticUniverse,
                                          snapshot: MaterialisedUniverse,
                                          table: str) -> None:
    assert getattr(snapshot, table).shape == getattr(generated, table).shape


def test_snapshot_round_trips_the_provider_api(generated: SyntheticUniverse,
                                               snapshot: MaterialisedUniverse) -> None:
    """Parquet returns dates as datetime64 while the engine compares against
    `datetime.date`. Left uncoerced these queries return empty rather than raising,
    which is the failure mode that would silently produce an empty index."""
    as_of = generated.calendar[-1].date()
    calls = [
        ("get_prices", (None, generated.start, as_of)),
        ("get_shares", (None, as_of)),
        ("get_shares_history", (None, generated.start, as_of)),
        ("get_corp_actions", (None, generated.start, as_of)),
        ("get_classifications", (None, as_of)),
        ("get_securities", ()),
        ("get_listings", ()),
        ("get_identifier_map", ()),
        ("get_issuers", ()),
    ]
    for method, args in calls:
        left = getattr(generated, method)(*args)
        right = getattr(snapshot, method)(*args)
        assert not left.empty, f"{method} returned nothing from the generator"
        assert left.shape == right.shape, method


def test_snapshot_span_matches(generated: SyntheticUniverse,
                               snapshot: MaterialisedUniverse) -> None:
    assert snapshot.start == generated.start
    assert snapshot.end == generated.end
    assert snapshot.n_days == len(generated.calendar)


# ------------------------------------------------------------------------------ identity


def test_fingerprints_are_stable_and_distinct(snapshot: MaterialisedUniverse,
                                              generated: SyntheticUniverse) -> None:
    """A snapshot hashes its bytes, a generator hashes its config. Both must be stable
    across calls - the manifest records one, and a fingerprint that moved between two
    identical builds would make every audit trail worthless."""
    assert snapshot.fingerprint == snapshot.fingerprint
    assert generated.fingerprint == generated.fingerprint
    assert snapshot.fingerprint != generated.fingerprint


def test_missing_tables_raise_rather_than_returning_empty(tmp_path: Path) -> None:
    """An incomplete snapshot must fail at open, not build a wrong index. An empty price
    table and a universe that genuinely never traded are indistinguishable downstream."""
    (tmp_path / "prices.parquet").write_bytes(b"not parquet")
    with pytest.raises(SnapshotError, match="incomplete"):
        MaterialisedUniverse(tmp_path)


def test_absent_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError):
        MaterialisedUniverse(tmp_path / "does-not-exist")


# ---------------------------------------------------------------------------- the point


def test_build_from_snapshot_matches_generator(generated: SyntheticUniverse,
                                               snapshot: MaterialisedUniverse) -> None:
    """The seam's actual contract: same numbers in, same index out.

    This is what makes real data possible. If a snapshot build and a generated build
    agree bit for bit on identical inputs, then swapping in a *differently sourced*
    snapshot changes only the data - never the methodology.
    """
    common = {
        "index_config": global_all_cap(),
        "start": BUILD_START,
        "end": BUILD_END,
        "validate": False,
    }
    from_generator = build_index(
        BuildSpec(universe_config=CONFIG, **common), verbose=False)  # type: ignore[arg-type]
    from_snapshot = build_index(
        BuildSpec(universe=snapshot, **common), verbose=False)  # type: ignore[arg-type]

    left = from_generator.history.levels.reset_index(drop=True)
    right = from_snapshot.history.levels.reset_index(drop=True)
    pd.testing.assert_frame_equal(left, right, check_exact=False, rtol=1e-12)


def test_manifest_identifies_a_snapshot_build_by_its_data(
        snapshot: MaterialisedUniverse) -> None:
    """A snapshot build must be identifiable years later by the data it used.

    `record_input` stores a hash, not the value, so the assertion is on *which* key is
    recorded: a generated build records `universe_config` (the config determines the
    data), a snapshot build records `universe` (only the bytes do). Recording a config
    for a snapshot build would be a lie - the config no longer determines anything.
    """
    common = {"index_config": global_all_cap(), "start": BUILD_START,
              "end": dt.date(2016, 6, 30), "validate": False}
    from_snapshot = build_index(
        BuildSpec(universe=snapshot, **common), verbose=False)  # type: ignore[arg-type]
    from_generator = build_index(
        BuildSpec(universe_config=CONFIG, **common), verbose=False)  # type: ignore[arg-type]

    assert "universe" in from_snapshot.manifest.input_hashes
    assert "universe_config" not in from_snapshot.manifest.input_hashes
    assert "universe_config" in from_generator.manifest.input_hashes

    # Same snapshot, same recorded hash - otherwise no audit trail is reproducible.
    again = build_index(
        BuildSpec(universe=snapshot, **common), verbose=False)  # type: ignore[arg-type]
    assert (again.manifest.input_hashes["universe"]
            == from_snapshot.manifest.input_hashes["universe"])
