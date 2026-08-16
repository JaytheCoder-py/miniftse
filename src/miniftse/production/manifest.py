"""Run manifests: making a published number reproducible years later.

Index providers get audited, and methodologies get challenged. "Reproduce the level you
published on 14 March 2023" is a question that has to have an answer, and the only way
to have one is to record, at the time, everything that determined the output:

* the **code** version (git SHA, and whether the tree was dirty)
* the **data** version (a content hash of every input, not a filename)
* the **parameters** (the full config, serialised)
* the **environment** (Python and library versions)

Hashing content rather than trusting filenames is the part people skip. A file called
``prices_2023-03-14.parquet`` can be silently rewritten; its SHA-256 cannot.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import pandas as pd

T = TypeVar("T")


def git_sha(repo: Path | None = None) -> tuple[str, bool]:
    """Current commit and whether the working tree is dirty.

    A dirty tree means the manifest cannot fully identify the code, so it is recorded
    explicitly rather than silently ignored. A production run from a dirty tree is a
    finding in itself.

    "Dirty" here means a tracked file has local modifications (`--untracked-files=no`),
    not merely that untracked files exist. An untracked file cannot have changed a
    tracked build input - by definition nothing in the committed tree references it -
    so counting it toward "dirty" would only ever produce false positives: a build
    directory the caller writes its own output into (this module's `desk/snapshot.py`
    caller is exactly such a case) would otherwise make every run from an
    otherwise-clean commit report dirty, permanently, for a reason that has nothing to
    do with what code produced the output.
    """
    cwd = str(repo) if repo else None
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
            check=True,
        ).stdout.strip()
        return sha, bool(status)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "unknown", True


def hash_frame(df: pd.DataFrame) -> str:
    """Content hash of a DataFrame, stable across row order and index labels.

    Sorting first matters: two frames with identical content in different order are the
    same input, and a hash that says otherwise makes every run look non-reproducible
    and trains people to ignore the check.
    """
    if df is None or df.empty:
        return "empty"
    ordered = df.sort_index(axis=1)
    cols = [c for c in ordered.columns if ordered[c].dtype != object] or list(ordered.columns)
    with contextlib.suppress(TypeError, ValueError):
        ordered = ordered.sort_values(list(ordered.columns[: min(3, len(cols))]))
    payload = pd.util.hash_pandas_object(ordered, index=False).to_numpy().tobytes()
    return hashlib.sha256(payload).hexdigest()[:32]


def hash_object(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:32]


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:32]


@dataclass
class RunManifest:
    """Everything needed to reproduce one run."""

    run_id: str
    created_at: str
    index_id: str
    as_of: str
    git_sha: str
    git_dirty: bool
    config_hash: str
    config: dict[str, Any]
    input_hashes: dict[str, str] = field(default_factory=dict)
    output_hashes: dict[str, str] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    status: str = "running"
    notes: list[str] = field(default_factory=list)

    @classmethod
    def start(
        cls,
        index_id: str,
        as_of: dt.date,
        config: Any,
        repo: Path | None = None,
    ) -> RunManifest:
        sha, dirty = git_sha(repo)
        payload = config.to_dict() if hasattr(config, "to_dict") else asdict(config)
        now = dt.datetime.now(dt.UTC)
        return cls(
            run_id=f"{index_id}-{as_of.isoformat()}-{now.strftime('%Y%m%dT%H%M%S')}",
            created_at=now.isoformat(),
            index_id=index_id,
            as_of=as_of.isoformat(),
            git_sha=sha,
            git_dirty=dirty,
            config_hash=hash_object(payload),
            config=payload,
            environment={
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "pandas": pd.__version__,
                "numpy": __import__("numpy").__version__,
            },
            notes=(
                [
                    "WARNING: run from a dirty working tree - the git SHA does not "
                    "fully identify the code that produced this output"
                ]
                if dirty
                else []
            ),
        )

    def record_input(self, name: str, data: pd.DataFrame | Path | Any) -> RunManifest:
        if isinstance(data, pd.DataFrame):
            self.input_hashes[name] = hash_frame(data)
        elif isinstance(data, Path):
            self.input_hashes[name] = hash_file(data)
        else:
            self.input_hashes[name] = hash_object(data)
        return self

    def record_output(self, name: str, data: pd.DataFrame | Path | Any) -> RunManifest:
        if isinstance(data, pd.DataFrame):
            self.output_hashes[name] = hash_frame(data)
        elif isinstance(data, Path):
            self.output_hashes[name] = hash_file(data)
        else:
            self.output_hashes[name] = hash_object(data)
        return self

    def record_metric(self, name: str, value: Any) -> RunManifest:
        self.metrics[name] = value
        return self

    def finish(self, status: str = "success", duration: float = 0.0) -> RunManifest:
        self.status = status
        self.duration_seconds = duration
        return self

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> RunManifest:
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def diff(self, other: RunManifest) -> dict[str, Any]:
        """Compare two runs. The first thing to do when output changes unexpectedly.

        It answers "did the code change, the data change, or the parameters change?" -
        which is the whole of root-cause analysis for a reproducibility failure, and
        usually takes five seconds instead of an afternoon.
        """
        out: dict[str, Any] = {}
        if self.git_sha != other.git_sha:
            out["code"] = {"this": self.git_sha[:12], "other": other.git_sha[:12]}
        if self.config_hash != other.config_hash:
            changed = {
                k: {"this": self.config.get(k), "other": other.config.get(k)}
                for k in set(self.config) | set(other.config)
                if self.config.get(k) != other.config.get(k)
            }
            out["config"] = changed
        input_changes = {
            k: {"this": self.input_hashes.get(k), "other": other.input_hashes.get(k)}
            for k in set(self.input_hashes) | set(other.input_hashes)
            if self.input_hashes.get(k) != other.input_hashes.get(k)
        }
        if input_changes:
            out["inputs"] = input_changes
        output_changes = {
            k: {"this": self.output_hashes.get(k), "other": other.output_hashes.get(k)}
            for k in set(self.output_hashes) | set(other.output_hashes)
            if self.output_hashes.get(k) != other.output_hashes.get(k)
        }
        if output_changes:
            out["outputs"] = output_changes
        return out

    def explain_diff(self, other: RunManifest) -> str:
        d = self.diff(other)
        if not d:
            return "The two runs are identical in code, configuration, inputs and outputs."
        lines = [f"Differences between {self.run_id} and {other.run_id}:"]
        if "code" in d:
            lines.append(f"  CODE changed: {d['code']['other'][:12]} -> {d['code']['this'][:12]}")
        if "config" in d:
            lines.append("  CONFIG changed:")
            lines += [f"    {k}: {v['other']} -> {v['this']}" for k, v in d["config"].items()]
        if "inputs" in d:
            lines.append(f"  INPUT data changed: {', '.join(d['inputs'])}")
        if "outputs" in d:
            lines.append(f"  OUTPUT changed: {', '.join(d['outputs'])}")
            if "code" not in d and "config" not in d and "inputs" not in d:
                lines.append(
                    "    Output changed with identical code, config and inputs. That "
                    "is non-determinism - an unseeded random number generator, a "
                    "dict-ordering dependency, or a timestamp leaking into the "
                    "calculation. Treat as a defect, not a curiosity."
                )
        return "\n".join(lines)


class ManifestStore:
    """Filesystem-backed manifest storage with lookup and replay."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, manifest: RunManifest) -> Path:
        return manifest.save(self.directory)

    def load(self, run_id: str) -> RunManifest:
        path = self.directory / f"{run_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"no manifest {run_id} in {self.directory}")
        return RunManifest.load(path)

    def list_runs(self, index_id: str | None = None) -> pd.DataFrame:
        rows = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                m = RunManifest.load(path)
            except (json.JSONDecodeError, TypeError):
                continue
            if index_id and m.index_id != index_id:
                continue
            rows.append(
                {
                    "run_id": m.run_id,
                    "index_id": m.index_id,
                    "as_of": m.as_of,
                    "created_at": m.created_at,
                    "status": m.status,
                    "git_sha": m.git_sha[:12],
                    "dirty": m.git_dirty,
                    "duration_s": m.duration_seconds,
                }
            )
        return pd.DataFrame(rows)

    def latest(self, index_id: str) -> RunManifest | None:
        runs = self.list_runs(index_id)
        if runs.empty:
            return None
        return self.load(runs.sort_values("created_at").iloc[-1]["run_id"])


def reproduce(
    manifest: RunManifest,
    rebuild: Callable[[dict[str, Any]], dict[str, pd.DataFrame]],
) -> dict[str, Any]:
    """Re-run from a manifest and verify the outputs hash identically.

    `rebuild` receives the recorded config and returns the outputs. Any hash mismatch is
    reported per output, so a partial failure is diagnosable rather than a single
    unhelpful boolean.
    """
    outputs = rebuild(manifest.config)
    results = {}
    for name, expected in manifest.output_hashes.items():
        if name not in outputs:
            results[name] = {"status": "missing", "expected": expected}
            continue
        actual = hash_frame(outputs[name])
        results[name] = {
            "status": "match" if actual == expected else "MISMATCH",
            "expected": expected,
            "actual": actual,
        }
    reproduced = all(r["status"] == "match" for r in results.values())
    return {
        "run_id": manifest.run_id,
        "reproduced": reproduced,
        "outputs": results,
        "note": (
            "Exact reproduction confirmed."
            if reproduced
            else "Reproduction FAILED. Check the git SHA and input hashes with "
            "explain_diff before assuming the code is at fault."
        ),
    }
