"""Golden-master regression testing.

Pin a full index history to disk with a hash, and fail CI on any drift.

This is the highest-value test in the repository and the one most specific to index
work. Unit tests check that a function does what its author intended; a golden master
checks that *ten years of published numbers have not moved*. For a product whose output
is a number other people trade against, that is a different and more important
guarantee - and a refactor that changes the index by half a basis point is a
recalculation event, not a tidy-up.

The tolerance is deliberately not zero. Floating-point summation order changes across
numpy versions and platforms, so an exact-equality test fails for reasons that have
nothing to do with correctness and gets disabled within a month. One tenth of a basis
point is far below any materiality threshold and far above floating-point noise.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from miniftse.production.manifest import hash_frame

DEFAULT_TOLERANCE_BPS = 0.1
"""Any level difference above this fails. Well below materiality, well above noise."""


@dataclass
class GoldenMaster:
    """A pinned index history plus the metadata to interpret a mismatch."""

    name: str
    levels: pd.DataFrame
    content_hash: str
    created_at: str
    git_sha: str
    config: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    tolerance_bps: float = DEFAULT_TOLERANCE_BPS

    @classmethod
    def create(
        cls,
        name: str,
        levels: pd.DataFrame,
        config: Any = None,
        tolerance_bps: float = DEFAULT_TOLERANCE_BPS,
    ) -> GoldenMaster:
        from miniftse.production.manifest import git_sha

        sha, _ = git_sha()
        payload = (config.to_dict() if hasattr(config, "to_dict")
                   else dict(config or {}))
        return cls(
            name=name, levels=levels.reset_index(drop=True),
            content_hash=hash_frame(levels), git_sha=sha,
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            config=payload, tolerance_bps=tolerance_bps,
            metrics=_metrics(levels),
        )

    def save(self, directory: Path) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        data_path = directory / f"{self.name}.parquet"
        meta_path = directory / f"{self.name}.json"

        frame = self.levels.copy()
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"])
        frame.to_parquet(data_path, index=False)

        meta_path.write_text(json.dumps({
            "name": self.name, "content_hash": self.content_hash,
            "created_at": self.created_at, "git_sha": self.git_sha,
            "config": self.config, "metrics": self.metrics,
            "tolerance_bps": self.tolerance_bps, "n_rows": len(self.levels),
        }, indent=2, default=str), encoding="utf-8")
        return data_path, meta_path

    @classmethod
    def load(cls, directory: Path, name: str) -> GoldenMaster:
        meta = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
        levels = pd.read_parquet(directory / f"{name}.parquet")
        if "date" in levels.columns:
            levels["date"] = pd.to_datetime(levels["date"]).dt.date
        return cls(
            name=meta["name"], levels=levels, content_hash=meta["content_hash"],
            created_at=meta["created_at"], git_sha=meta["git_sha"],
            config=meta.get("config", {}), metrics=meta.get("metrics", {}),
            tolerance_bps=meta.get("tolerance_bps", DEFAULT_TOLERANCE_BPS),
        )


@dataclass
class ComparisonResult:
    passed: bool
    n_compared: int
    max_diff_bps: float
    mean_diff_bps: float
    first_divergence: dt.date | None
    divergent_dates: list[dt.date]
    missing_dates: list[dt.date]
    extra_dates: list[dt.date]
    column_diffs: dict[str, float]
    message: str

    def report(self) -> str:
        lines = [self.message]
        if self.first_divergence:
            lines.append(
                f"  First divergence: {self.first_divergence}. Start the "
                "investigation there - later differences are almost always downstream "
                "of the first, because the divisor carries the error forward."
            )
        if self.divergent_dates:
            shown = ", ".join(str(d) for d in self.divergent_dates[:8])
            more = (f" (+{len(self.divergent_dates) - 8} more)"
                    if len(self.divergent_dates) > 8 else "")
            lines.append(f"  Divergent dates: {shown}{more}")
        if self.missing_dates:
            lines.append(f"  {len(self.missing_dates)} dates in the golden master are "
                         "absent from the new run")
        if self.extra_dates:
            lines.append(f"  {len(self.extra_dates)} dates in the new run are absent "
                         "from the golden master")
        if self.column_diffs:
            lines.append("  Largest difference per column (bp):")
            lines += [f"    {k}: {v:.4f}" for k, v in
                      sorted(self.column_diffs.items(), key=lambda kv: -kv[1])]
        return "\n".join(lines)


def compare(
    golden: GoldenMaster,
    candidate: pd.DataFrame,
    columns: tuple[str, ...] = ("price_return", "gross_total_return",
                                "net_total_return", "divisor"),
) -> ComparisonResult:
    """Compare a fresh run against the pinned history."""
    g = golden.levels.copy()
    c = candidate.copy()
    for frame in (g, c):
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"]).dt.date

    g = g.set_index("date")
    c = c.set_index("date")
    common = g.index.intersection(c.index)
    missing = [d for d in g.index if d not in c.index]
    extra = [d for d in c.index if d not in g.index]

    if len(common) == 0:
        return ComparisonResult(
            False, 0, float("inf"), float("inf"), None, [], missing, extra, {},
            "GOLDEN MASTER FAILED: no overlapping dates between the pinned history "
            "and the new run.",
        )

    column_diffs: dict[str, float] = {}
    worst_series = pd.Series(0.0, index=common)
    for col in columns:
        if col not in g.columns or col not in c.columns:
            continue
        base = g.loc[common, col].astype(float)
        new = c.loc[common, col].astype(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            diff = np.where(base != 0, np.abs(new / base - 1.0) * 10_000, 0.0)
        diff_series = pd.Series(diff, index=common).fillna(0.0)
        column_diffs[col] = float(diff_series.max())
        worst_series = np.maximum(worst_series, diff_series)

    divergent = [d for d in common if worst_series[d] > golden.tolerance_bps]
    max_diff = float(worst_series.max())
    passed = not divergent and not missing and not extra

    if passed:
        message = (
            f"Golden master matched: {len(common)} dates, largest difference "
            f"{max_diff:.6f}bp against a tolerance of {golden.tolerance_bps}bp."
        )
    else:
        message = (
            f"GOLDEN MASTER FAILED: {len(divergent)} of {len(common)} dates exceed the "
            f"{golden.tolerance_bps}bp tolerance; largest difference {max_diff:.4f}bp. "
            "If this change is intended, re-pin the master and record why in "
            "DECISIONS.md - an index history does not change by accident."
        )

    return ComparisonResult(
        passed=passed, n_compared=len(common), max_diff_bps=max_diff,
        mean_diff_bps=float(worst_series.mean()),
        first_divergence=divergent[0] if divergent else None,
        divergent_dates=divergent, missing_dates=missing, extra_dates=extra,
        column_diffs=column_diffs, message=message,
    )


def _metrics(levels: pd.DataFrame) -> dict[str, Any]:
    if levels.empty:
        return {}
    out: dict[str, Any] = {"n_rows": len(levels)}
    for col in ("price_return", "gross_total_return", "net_total_return", "divisor"):
        if col in levels.columns:
            out[f"final_{col}"] = float(levels[col].iloc[-1])
    if "date" in levels.columns:
        out["start"] = str(levels["date"].iloc[0])
        out["end"] = str(levels["date"].iloc[-1])
    return out


def introduce_regression(levels: pd.DataFrame, bps: float = 0.5,
                         from_index: int = 500) -> pd.DataFrame:
    """Perturb a history, to prove the golden master actually catches drift.

    A regression test nobody has watched fail is a regression test of unknown value.
    The test suite uses this to confirm the master detects a half-basis-point shift -
    a change far too small to notice by eye and far too large to publish.
    """
    out = levels.copy()
    for col in ("price_return", "gross_total_return", "net_total_return"):
        if col in out.columns:
            out.loc[from_index:, col] = out.loc[from_index:, col] * (1 + bps / 10_000)
    return out
