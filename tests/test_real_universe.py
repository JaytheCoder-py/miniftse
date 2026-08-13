"""Guards on the real-data ETL. No network: every remote call is stubbed.

Both tests here exist because the failures happened. A rate-limited Yahoo run returned
"possibly delisted; no price data found" for AAPL, MSFT and NVDA, one ticker out of two
hundred survived, and the ETL wrote a one-security snapshot over a known-good one. The
resulting index built cleanly and was entirely wrong.

The lesson is specific: for this pipeline a partial result is more dangerous than a
failure, because a snapshot covering 0.5% of its universe still produces a plausible
index history. So the ETL must refuse to write, and must never damage what is already
on disk while finding that out.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from miniftse.data.real import (
    RealDataError,
    RealUniverseBuilder,
    RealUniverseConfig,
    sic_to_icb,
)

TICKERS = [f"T{n:03d}" for n in range(20)]


def _config(tmp_path: Path, **overrides: Any) -> RealUniverseConfig:
    defaults: dict[str, Any] = {
        "n_securities": len(TICKERS),
        "start": dt.date(2020, 1, 1),
        "end": dt.date(2020, 3, 31),
        "cache_dir": tmp_path / "cache",
        "price_retries": 1,
        "price_backoff_seconds": 0.0,
        "contact": "miniftse-test/0.1 (contact: test@example.com)",
    }
    defaults.update(overrides)
    return RealUniverseConfig(**defaults)


def _candidates() -> pd.DataFrame:
    return pd.DataFrame({
        "issuer_id": [str(n).zfill(10) for n in range(len(TICKERS))],
        "ticker": TICKERS,
        "name": [f"Company {t}" for t in TICKERS],
        "rank": list(range(len(TICKERS))),
    })


def _price_frame() -> pd.DataFrame:
    index = pd.bdate_range("2020-01-01", "2020-03-31")
    return pd.DataFrame(
        {"Close": 100.0, "Open": 100.0, "High": 101.0, "Low": 99.0, "Volume": 1000.0,
         "Dividends": 0.0, "Stock Splits": 0.0},
        index=index,
    )


def _stub_prices(builder: RealUniverseBuilder, succeed: set[str]) -> None:
    """Make `_fetch_one` succeed only for `succeed`, as a throttled Yahoo would."""
    def fetch_one(ticker: str) -> pd.DataFrame | None:
        return _price_frame() if ticker in succeed else None
    builder._fetch_one = fetch_one  # type: ignore[method-assign]


# ------------------------------------------------------------------- the coverage floor


def test_a_throttled_run_refuses_to_write(tmp_path: Path) -> None:
    """One survivor out of twenty must raise, not produce a one-security snapshot."""
    builder = RealUniverseBuilder(config=_config(tmp_path), verbose=False)
    _stub_prices(builder, succeed={TICKERS[0]})
    listing_ids = {t: f"{t}.XNAS" for t in TICKERS}

    with pytest.raises(RealDataError, match="below the"):
        builder.prices_and_actions(_candidates(), listing_ids)


def test_full_coverage_is_accepted(tmp_path: Path) -> None:
    builder = RealUniverseBuilder(config=_config(tmp_path), verbose=False)
    _stub_prices(builder, succeed=set(TICKERS))
    listing_ids = {t: f"{t}.XNAS" for t in TICKERS}

    prices, _ = builder.prices_and_actions(_candidates(), listing_ids)
    assert prices["security_id"].nunique() == len(TICKERS)


def test_the_floor_is_where_the_config_puts_it(tmp_path: Path) -> None:
    """Losing a handful of names is normal; losing most of the universe is not."""
    survivors = set(TICKERS[:17])  # 85%
    listing_ids = {t: f"{t}.XNAS" for t in TICKERS}

    lenient = RealUniverseBuilder(
        config=_config(tmp_path, min_price_coverage=0.80), verbose=False)
    _stub_prices(lenient, survivors)
    prices, _ = lenient.prices_and_actions(_candidates(), listing_ids)
    assert prices["security_id"].nunique() == 17

    strict = RealUniverseBuilder(
        config=_config(tmp_path, min_price_coverage=0.95), verbose=False)
    _stub_prices(strict, survivors)
    with pytest.raises(RealDataError, match="85.0%"):
        strict.prices_and_actions(_candidates(), listing_ids)


# ----------------------------------------------------------------------- atomic writes


def test_a_failed_rebuild_leaves_the_previous_snapshot_intact(tmp_path: Path) -> None:
    """The bug that cost a good snapshot: writing in place.

    A rate-limited run got far enough to overwrite `prices.parquet` before anything
    noticed it had one security in it. Other work depends on a snapshot, so a failed
    rebuild must leave the previous one byte-for-byte unchanged.
    """
    dest = tmp_path / "snapshot"
    dest.mkdir()
    (dest / "prices.parquet").write_bytes(b"the good snapshot")
    (dest / "config.json").write_text(json.dumps({"securities": 199}))
    before = {p.name: p.read_bytes() for p in dest.iterdir()}

    builder = RealUniverseBuilder(config=_config(tmp_path), verbose=False)
    builder.candidates = _candidates  # type: ignore[method-assign]
    builder.reference = lambda candidates: {  # type: ignore[method-assign, assignment]
        "securities": pd.DataFrame({"security_id": TICKERS}),
        "listings": pd.DataFrame({"security_id": TICKERS,
                                  "listing_id": [f"{t}.XNAS" for t in TICKERS]}),
        "identifiers": pd.DataFrame({"security_id": TICKERS}),
        "classifications": pd.DataFrame({"security_id": TICKERS}),
    }
    _stub_prices(builder, succeed={TICKERS[0]})  # throttled

    with pytest.raises(RealDataError):
        builder.build(dest)

    after = {p.name: p.read_bytes() for p in dest.iterdir()}
    assert after == before, "a failed rebuild damaged the existing snapshot"


def test_no_staging_directory_is_left_behind_on_failure(tmp_path: Path) -> None:
    dest = tmp_path / "snapshot"
    builder = RealUniverseBuilder(config=_config(tmp_path), verbose=False)
    builder.candidates = _candidates  # type: ignore[method-assign]
    builder.reference = lambda candidates: {  # type: ignore[method-assign, assignment]
        "securities": pd.DataFrame({"security_id": TICKERS}),
        "listings": pd.DataFrame({"security_id": TICKERS,
                                  "listing_id": [f"{t}.XNAS" for t in TICKERS]}),
        "identifiers": pd.DataFrame({"security_id": TICKERS}),
        "classifications": pd.DataFrame({"security_id": TICKERS}),
    }
    _stub_prices(builder, succeed=set())

    with pytest.raises(RealDataError):
        builder.build(dest)
    assert not dest.exists()


# ------------------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("sic", "icb"),
    [
        (2834, "20"),   # pharmaceutical preparations -> health care
        (8000, "20"),   # health services
        (1311, "60"),   # crude petroleum -> energy
        (4911, "65"),   # electric services -> utilities
        (4813, "15"),   # telephone communications
        (6798, "35"),   # REIT -> real estate, not financials
        (6021, "30"),   # national commercial banks -> financials
        (7372, "10"),   # prepackaged software -> technology
        (3711, "40"),   # motor vehicles -> consumer discretionary
        (3721, "50"),   # aircraft -> industrials
        (None, "50"),   # unclassifiable falls back to industrials
    ],
)
def test_sic_maps_to_the_expected_icb_industry(sic: int | None, icb: str) -> None:
    assert sic_to_icb(sic) == icb
