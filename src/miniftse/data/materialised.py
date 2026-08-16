"""A universe loaded from a parquet snapshot on disk.

`SyntheticUniverse.materialise()` already defined this layout; this module promotes it
from an output format to an input one. That single change is what lets the same index
engine run on generated and real data, because a snapshot has no opinion about where
its numbers came from.

Two consumers, and the second is the point:

* **Frozen synthetic.** Materialise once, load thereafter. Builds skip regeneration.
* **Real data.** `data.real` writes this layout from EDGAR and Yahoo. Nothing
  downstream of `data/` can tell the difference, which is the property
  `data.providers` claims in its opening paragraph and did not previously have.

Reproducibility survives the move to real data because of *when* the network is used.
Fetching happens once, in `data.real`, and produces files. Building reads files. So a
real build is as deterministic as a synthetic one for as long as the snapshot is kept -
and `fingerprint` hashes the file contents, so a build whose data changed underneath it
cannot silently claim to be the same run.

Date handling
-------------
Parquet has a date type but pandas reads it back as ``datetime64``, while the engine
compares against ``datetime.date``. Left alone, ``df["date"] >= start`` raises or - much
worse - silently compares wrong. `_DATE_COLUMNS` lists every column that must be coerced
back on load, and the coercion is applied centrally rather than at each call site.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from functools import cached_property
from pathlib import Path
from typing import Any

import pandas as pd

# Every column across every table that carries a date rather than a timestamp.
_DATE_COLUMNS: frozenset[str] = frozenset(
    {
        "date",
        "effective_date",
        "knowledge_date",
        "filed_date",
        "period_end",
        "announcement_date",
        "ex_date",
        "pay_date",
        "listing_start",
        "listing_end",
        "valid_from",
        "valid_to",
    }
)

# The tables `materialise()` writes. `factor_returns` is synthetic-only - it is the
# ground truth of the generative model, and real data has no such thing - so it is
# optional and absent from real snapshots.
_REQUIRED_TABLES: tuple[str, ...] = (
    "prices",
    "shares",
    "corp_actions",
    "securities",
    "listings",
    "identifiers",
    "fundamentals",
    "fx",
)
_OPTIONAL_TABLES: tuple[str, ...] = ("factor_returns", "classifications")


class SnapshotError(RuntimeError):
    """A snapshot is missing, incomplete, or internally inconsistent.

    Raised rather than returning empty frames. An empty price table and a table for a
    universe that genuinely had no trading are indistinguishable downstream, and that
    ambiguity is exactly how a silently-broken build publishes a wrong index level.
    """


def _coerce_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Return `df` with every known date column as `datetime.date` objects."""
    out = df.copy()
    for col in out.columns:
        if col in _DATE_COLUMNS and not out[col].empty:
            converted = pd.to_datetime(out[col], errors="coerce")
            # `.dt.date` on an all-NaT column yields NaT, which is what an open-ended
            # `valid_to` or a still-listed `listing_end` should be.
            out[col] = converted.dt.date
    return out


class MaterialisedUniverse:
    """A `UniverseData` backed by parquet files rather than a generator.

    Constructed from a directory written by `SyntheticUniverse.materialise()` or by
    `data.real.RealUniverseBuilder`. Tables are loaded lazily and cached, so opening a
    snapshot to read one table does not pay for all nine.
    """

    def __init__(self, path: Path | str, name: str | None = None) -> None:
        self.path = Path(path)
        if not self.path.is_dir():
            raise SnapshotError(f"no snapshot directory at {self.path}")

        missing = [t for t in _REQUIRED_TABLES if not (self.path / f"{t}.parquet").exists()]
        if missing:
            raise SnapshotError(
                f"snapshot at {self.path} is incomplete - missing {', '.join(missing)}. "
                f"A partial snapshot builds a wrong index rather than failing, so this "
                f"is checked at open time."
            )

        self._cache: dict[str, pd.DataFrame] = {}
        self._meta: dict[str, Any] = {}
        meta_path = self.path / "config.json"
        if meta_path.exists():
            self._meta = json.loads(meta_path.read_text())
        self._name = name or str(self._meta.get("name", f"snapshot:{self.path.name}"))

    # ------------------------------------------------------------------ loading

    def table(self, name: str) -> pd.DataFrame:
        """Load one table by name, cached, with date columns coerced."""
        if name in self._cache:
            return self._cache[name]
        dest = self.path / f"{name}.parquet"
        if not dest.exists():
            if name in _OPTIONAL_TABLES:
                self._cache[name] = pd.DataFrame()
                return self._cache[name]
            raise SnapshotError(f"snapshot {self.path} has no table '{name}'")
        self._cache[name] = _coerce_dates(pd.read_parquet(dest))
        return self._cache[name]

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        return self._name

    @cached_property
    def fingerprint(self) -> str:
        """SHA-256 over the snapshot's files, truncated to 16 hex chars.

        Content-addressed rather than config-addressed: a snapshot has no config that
        determines it, so the only honest identity is the bytes. Re-fetching real data
        that has been revised produces a different fingerprint, which is the point -
        the manifest should not claim two builds used the same data when they did not.
        """
        digest = hashlib.sha256()
        for table in sorted(_REQUIRED_TABLES + _OPTIONAL_TABLES):
            dest = self.path / f"{table}.parquet"
            if dest.exists():
                digest.update(table.encode())
                digest.update(dest.read_bytes())
        return digest.hexdigest()[:16]

    # ------------------------------------------------------------------ tables

    @property
    def prices(self) -> pd.DataFrame:
        return self.table("prices")

    @property
    def shares(self) -> pd.DataFrame:
        return self.table("shares")

    @property
    def corp_actions(self) -> pd.DataFrame:
        return self.table("corp_actions")

    @property
    def fundamentals(self) -> pd.DataFrame:
        return self.table("fundamentals")

    @property
    def fx_rates(self) -> pd.DataFrame:
        return self.table("fx")

    # ------------------------------------------------------------------ span

    @cached_property
    def calendar(self) -> pd.DatetimeIndex:
        """Every session with at least one price observation, ascending."""
        dates = pd.to_datetime(pd.Series(sorted(self.prices["date"].unique())))
        return pd.DatetimeIndex(dates)

    @property
    def n_days(self) -> int:
        return len(self.calendar)

    @property
    def start(self) -> dt.date:
        return (
            dt.date.fromisoformat(str(self._meta["start"]))
            if "start" in self._meta
            else self.calendar[0].date()
        )

    @property
    def end(self) -> dt.date:
        return (
            dt.date.fromisoformat(str(self._meta["end"]))
            if "end" in self._meta
            else self.calendar[-1].date()
        )

    # ------------------------------------------------------------------ provider API

    def get_prices(
        self, listing_ids: list[str] | None, start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        df = self.prices
        out = df[(df["date"] >= start) & (df["date"] <= end)]
        if listing_ids is not None:
            out = out[out["listing_id"].isin(listing_ids)]
        return out.reset_index(drop=True)

    def get_shares(self, security_ids: list[str] | None, as_of: dt.date) -> pd.DataFrame:
        df = self.shares
        known = df[df["knowledge_date"] <= as_of]
        if security_ids is not None:
            known = known[known["security_id"].isin(security_ids)]
        return (
            known.sort_values(["security_id", "effective_date", "knowledge_date"])
            .groupby("security_id", as_index=False)
            .last()
            .reset_index(drop=True)
        )

    def get_shares_history(
        self, security_ids: list[str] | None, start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        df = self.shares
        out = df[(df["effective_date"] >= start) & (df["effective_date"] <= end)]
        if security_ids is not None:
            out = out[out["security_id"].isin(security_ids)]
        return out.reset_index(drop=True)

    def get_fundamentals(
        self,
        security_ids: list[str] | None,
        items: list[str],
        as_of: dt.date,
        max_staleness_days: int = 550,
    ) -> pd.DataFrame:
        df = self.fundamentals
        if df.empty:
            return df
        known = df[(df["filed_date"] <= as_of) & (df["item"].isin(items))]
        known = known[known["period_end"] >= as_of - dt.timedelta(days=max_staleness_days)]
        if security_ids is not None:
            known = known[known["security_id"].isin(security_ids)]
        return (
            known.sort_values(["security_id", "item", "period_end", "filed_date"])
            .groupby(["security_id", "item"], as_index=False)
            .last()
            .reset_index(drop=True)
        )

    def get_fundamentals_ttm(
        self, security_ids: list[str] | None, item: str, as_of: dt.date
    ) -> pd.DataFrame:
        df = self.fundamentals
        if df.empty:
            return df
        known = df[(df["filed_date"] <= as_of) & (df["item"] == item)]
        if security_ids is not None:
            known = known[known["security_id"].isin(security_ids)]
        latest = (
            known.sort_values(["security_id", "period_end", "filed_date"])
            .groupby(["security_id", "period_end"], as_index=False)
            .last()
        )
        latest = latest.sort_values(["security_id", "period_end"])
        top4 = latest.groupby("security_id").tail(4)
        agg = top4.groupby("security_id", as_index=False).agg(
            value=("value", "sum"),
            n_periods=("value", "size"),
            latest_period=("period_end", "max"),
        )
        return agg[agg["n_periods"] == 4].reset_index(drop=True)

    def get_fundamentals_raw(self) -> pd.DataFrame:
        """The whole fundamentals table. See the warning on the synthetic namesake:
        using this without a `filed_date` bound is a look-ahead bug."""
        return self.fundamentals

    def get_corp_actions(
        self, security_ids: list[str] | None, start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        df = self.corp_actions
        if df.empty:
            return df
        out = df[(df["ex_date"] >= start) & (df["ex_date"] <= end)]
        if security_ids is not None:
            out = out[out["security_id"].isin(security_ids)]
        return out.reset_index(drop=True)

    def get_fx(self, base: str, quotes: list[str], start: dt.date, end: dt.date) -> pd.DataFrame:
        df = self.fx_rates
        out = df[(df["date"] >= start) & (df["date"] <= end) & (df["quote"].isin(quotes))]
        if "base" in out.columns:
            out = out[out["base"] == base]
        return out[["date", "base", "quote", "rate"]].reset_index(drop=True)

    def get_deposit_rates(
        self, currencies: list[str], start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        df = self.fx_rates
        out = df[(df["date"] >= start) & (df["date"] <= end) & (df["quote"].isin(currencies))]
        return (
            out[["date", "quote", "deposit_rate"]]
            .rename(columns={"quote": "currency"})
            .reset_index(drop=True)
        )

    def get_classifications(self, security_ids: list[str] | None, as_of: dt.date) -> pd.DataFrame:
        """Classifications, from the dedicated table if present or derived from
        `securities` if not - which is what a synthetic snapshot leaves behind."""
        del as_of
        table = self.table("classifications")
        if table.empty:
            secs = self.table("securities")
            table = secs[["security_id", "icb_industry"]].copy()
            table["effective_date"] = self.start
            table["knowledge_date"] = self.start
            table["icb_supersector"] = table["icb_industry"].astype(str) + "10"
        if security_ids is not None:
            table = table[table["security_id"].isin(security_ids)]
        return table.reset_index(drop=True)

    def get_issuers(self) -> pd.DataFrame:
        secs = self.table("securities")
        cols = [c for c in ("issuer_id", "country", "market_status") if c in secs.columns]
        return secs[cols].drop_duplicates("issuer_id").reset_index(drop=True)

    def get_securities(self) -> pd.DataFrame:
        return self.table("securities").copy()

    def get_listings(self) -> pd.DataFrame:
        return self.table("listings").copy()

    def get_identifier_map(self) -> pd.DataFrame:
        return self.table("identifiers").copy()

    # ------------------------------------------------------------------ output

    def materialise(self, path: Path) -> dict[str, Path]:
        """Copy this snapshot to `path`. Present so a `UniverseData` can always be
        re-emitted, whatever its origin."""
        path.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        for table in _REQUIRED_TABLES + _OPTIONAL_TABLES:
            src = self.path / f"{table}.parquet"
            if src.exists():
                dest = path / f"{table}.parquet"
                dest.write_bytes(src.read_bytes())
                written[table] = dest
        meta = dict(self._meta)
        meta.setdefault("name", self.name)
        (path / "config.json").write_text(json.dumps(meta, indent=2, default=str))
        return written

    def summary(self) -> dict[str, Any]:
        secs = self.table("securities")
        ca = self.corp_actions
        delisted = 0
        if "listing_end" in secs.columns:
            delisted = int(secs["listing_end"].notna().sum())
        return {
            "fingerprint": self.fingerprint,
            "name": self.name,
            "securities": int(len(secs)),
            "trading_days": self.n_days,
            "price_rows": int(len(self.prices)),
            "delisted": delisted,
            "corp_actions": (ca["event_type"].value_counts().to_dict() if not ca.empty else {}),
            "fundamental_rows": int(len(self.fundamentals)),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            # Both are absent from a snapshot the generator materialised, and their
            # absence is what tells a downstream document it is looking at simulated
            # data. See `reporting.factsheet._important_information`.
            **({"source": self._meta["source"]} if "source" in self._meta else {}),
            **({"provenance": self._meta["provenance"]} if "provenance" in self._meta else {}),
        }


__all__ = ["MaterialisedUniverse", "SnapshotError"]
