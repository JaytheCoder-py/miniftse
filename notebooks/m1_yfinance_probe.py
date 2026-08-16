"""Fetch-and-cache helper for probing what a free price API actually returns.

Plumbing only: it pulls each ticker three ways (unadjusted, auto-adjusted, share
counts) and caches to parquet so the comparison can be iterated on without
re-hitting the API. What the comparison found is recorded in the ``YahooProvider``
docstring in `miniftse/data/vendors.py` and in `miniftse/data/real.py` — split
adjustment destroys the as-traded series, delisted tickers return nothing at all,
and spin-offs appear in neither the dividends nor the splits column.

Usage:
    uv run python notebooks/m1_yfinance_probe.py
    uv run python notebooks/m1_yfinance_probe.py --refresh   # ignore cache
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yfinance as yf

CACHE = Path(__file__).resolve().parents[1] / "data" / "raw" / "m1_probe"

START = "2018-01-01"
END = "2026-08-01"


@dataclass(frozen=True)
class Case:
    """A ticker plus the specific data pathology it is here to expose."""

    ticker: str
    why: str


CASES: list[Case] = [
    # --- controls: nothing unusual, use these as your baseline ---
    Case("AAPL", "control - large, liquid, ordinary dividend payer"),
    Case("MSFT", "control - ditto"),
    # --- splits ---
    Case("NVDA", "10:1 forward split, 2024-06-10"),
    Case("AMZN", "20:1 forward split, 2022-06-06"),
    Case("GE", "1-for-8 REVERSE split, 2021-08-02"),
    # --- spin-offs: the parent price drops but no split/dividend is recorded ---
    Case("GE", "spun off GEHC 2023-01, GEV 2024-04 (also listed above for the reverse split)"),
    Case("GEHC", "the 2023 GE HealthCare spinco"),
    Case("GEV", "the 2024 GE Vernova spinco"),
    Case("MMM", "spun off Solventum (SOLV) 2024-04-01"),
    Case("SOLV", "the 3M spinco"),
    Case("K", "Kellanova - 2023 Kellogg split into K/KLG, then acquired by Mars: BOTH pathologies"),
    # --- dual class: same issuer, two lines, different ISINs ---
    Case("GOOGL", "Alphabet class A"),
    Case("GOOG", "Alphabet class C - same issuer, different security"),
    # --- delisted / acquired: survivorship bias lives here ---
    Case("TWTR", "taken private 2022-10-27"),
    Case("ATVI", "acquired by Microsoft 2023-10-13"),
    Case("VMW", "acquired by Broadcom 2023-11-22"),
    # --- depositary receipt ---
    Case("BABA", "US ADR; the primary line is 9988.HK - one issuer, two very different objects"),
]


def _pull(ticker: str) -> dict[str, pd.DataFrame]:
    """Pull the three views of a ticker the comparison needs."""
    t = yf.Ticker(ticker)
    out: dict[str, pd.DataFrame] = {}

    # auto_adjust=False: yfinance calls this "unadjusted". Verify that claim.
    out["raw"] = t.history(start=START, end=END, auto_adjust=False, actions=True)
    # auto_adjust=True: the default. Close is overwritten with the adjusted series.
    out["auto"] = t.history(start=START, end=END, auto_adjust=True, actions=True)

    try:
        shares = t.get_shares_full(start=START, end=END)
        out["shares"] = (
            shares.to_frame("shares") if isinstance(shares, pd.Series) else pd.DataFrame()
        )
    except Exception as exc:  # noqa: BLE001 - the failure mode is itself a finding
        out["shares"] = pd.DataFrame()
        print(f"  ! {ticker}: shares lookup failed: {type(exc).__name__}: {exc}")

    return out


def fetch(refresh: bool = False) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, object]] = {}

    for case in dict.fromkeys(c.ticker for c in CASES):
        dest = CACHE / case
        dest.mkdir(exist_ok=True)

        if not refresh and (dest / "raw.parquet").exists():
            print(f"  = {case}: cached")
            continue

        print(f"  > {case}: fetching")
        frames = _pull(case)
        for name, df in frames.items():
            if df.empty:
                print(f"  ! {case}: '{name}' came back EMPTY - that is a finding, write it down")
                continue
            # parquet cannot hold tz-aware DatetimeIndex from every backend cleanly
            df = df.copy()
            df.index = pd.to_datetime(df.index, utc=True).tz_localize(None)
            df.to_parquet(dest / f"{name}.parquet")

        manifest[case] = {
            name: {"rows": len(df), "first": str(df.index.min()), "last": str(df.index.max())}
            for name, df in frames.items()
            if not df.empty
        }

    if manifest:
        (CACHE / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nCached to {CACHE}")


def load(ticker: str, view: str = "raw") -> pd.DataFrame:
    """Load a cached frame. view in {'raw', 'auto', 'shares'}."""
    path = CACHE / ticker / f"{view}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"no cache for {ticker}/{view} - run this script first")
    return pd.read_parquet(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="ignore cache and re-fetch")
    args = parser.parse_args()

    print(f"{len(CASES)} cases, {len(set(c.ticker for c in CASES))} unique tickers\n")
    fetch(refresh=args.refresh)
