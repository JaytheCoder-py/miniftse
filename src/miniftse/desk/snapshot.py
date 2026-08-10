"""Precompute every artefact the ops desk serves.

One reference `build_index` run produces the lot. The deployed application loads these
files at startup and never rebuilds anything: a cold start that recomputes ten years of
index history is a failed demo, and the visitor who clicked the link from a CV is gone
before it finishes.

The rule this module lives by is **fail fast, or write nothing**. Every input that lives
outside the build is read before the build starts, every helper raises on empty input
rather than emitting a plausible-looking empty file, and `manifest.json` is written last.
Its presence is the signal that the snapshot is complete; a half-written snapshot must
never reach the repository.

Run it with `miniftse desk-snapshot` or `make desk-data`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from miniftse.agents.evals import EvalCase, load_eval_set, run_evals
from miniftse.agents.rag import MethodologyAssistant
from miniftse.calc.index import IndexHistory
from miniftse.config import global_all_cap
from miniftse.data.synthetic import SyntheticConfig
from miniftse.production.build import BuildResult, BuildSpec, build_index
from miniftse.production.daily import IndexStateFile
from miniftse.production.golden import GoldenMaster, compare
from miniftse.production.manifest import git_sha
from miniftse.quality.faults import baseline_from_build, drill_summary, run_chaos_drill
from miniftse.quality.rules import ValidationContext
from miniftse.weighting.schemes import SCHEME_PROPERTIES

REPO_ROOT = Path(__file__).resolve().parents[3]
"""`src/miniftse/desk/snapshot.py` -> the repository root. The snapshot reads documents
that live outside the package - the ground rules, the memos, the generated one-pagers
and the pinned golden master - because those are repository content, not library data."""

ARTEFACTS_DIR = REPO_ROOT / "artefacts"
GROUND_RULES_DIR = REPO_ROOT / "ground_rules"
MEMOS_DIR = REPO_ROOT / "memos"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"
GOLDEN_NAME = "reference"
EVAL_SET_PATH = ARTEFACTS_DIR / "eval_set.json"

DRILL_SEED = 20260809
"""The seed the precomputed chaos drill runs at. The live drill on `/chaos` defaults to
the same value so a visitor can confirm the two agree."""

EXPECTED_FILES: tuple[str, ...] = (
    "overview.json",
    "constituents.json",
    "capacity.json",
    "risk_attribution.json",
    "days.parquet",
    "divisor_audit.parquet",
    "reviews.parquet",
    "chaos_baseline/meta.json",
    "chaos_precomputed.json",
    "golden_diff.json",
    "evals.json",
    "manifest.json",
)
"""Everything a complete snapshot contains. The application validates the directory
against this same tuple at startup and refuses to serve half a desk."""

REVIEW_COLUMNS: tuple[str, ...] = (
    "date", "n_before", "n_after", "n_additions", "n_deletions", "one_way_turnover",
    "additions_weight", "deletions_weight", "divisor_before", "divisor_after",
    "level_continuity_bps",
)
"""The shape `IndexCalculator._apply_review` produces. Written explicitly so a build
window containing no review still yields a frame the day screen can join against."""


class SnapshotError(RuntimeError):
    """A build produced something the desk cannot serve."""


@dataclass(frozen=True)
class SnapshotManifest:
    """What was built, from what code, when, and what it hashes to."""

    git_sha: str
    git_dirty: bool
    created_at: str
    index_id: str
    build_spec: dict[str, Any]
    duration_seconds: float
    files: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def reference_spec(
    securities: int = 300,
    start: dt.date = dt.date(2016, 1, 4),
    end: dt.date = dt.date(2024, 12, 31),
    seed: int = 20260809,
) -> BuildSpec:
    """The build the deployed snapshot is made from.

    Nine years and 300 securities: long enough that the reviews, spin-offs and rights
    issues the day screen exists to explain actually occur, small enough that
    `make desk-data` finishes while you are still looking at it.
    """
    return BuildSpec(
        index_config=global_all_cap(),
        universe_config=SyntheticConfig(n_securities=securities, seed=seed),
        start=start,
        end=end,
    )


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def build_snapshot(out_dir: Path, spec: BuildSpec | None = None) -> SnapshotManifest:
    """Build every artefact the ops desk serves into `out_dir`.

    Raises rather than writing a partial snapshot: missing inputs are detected before
    the build runs, empty outputs are detected before they are written, and
    `manifest.json` lands only once everything else has.
    """
    started = time.perf_counter()
    spec = spec or reference_spec()
    out_dir = Path(out_dir)

    # Read every out-of-build input first. A missing one-pager should cost a second,
    # not a build.
    onepagers = _read_onepagers()
    eval_cases = _read_eval_set()
    corpus = _corpus_directories()

    result = build_index(spec, verbose=False)
    index_id = str(result.manifest.index_id)
    levels = result.history.levels
    weights = result.history.weights
    # `IndexHistory.divisor_audit` is the engine's audit frame, taken at the end of the
    # run - the same object `calculator.engine.audit_frame()` rebuilds, already typed.
    audit = result.history.divisor_audit

    if levels.empty:
        raise SnapshotError("the build produced no index levels; nothing to serve")
    if weights.empty:
        raise SnapshotError("the build produced no weight snapshots; the index screen "
                            "and the chaos baseline both need them")
    if audit.empty:
        raise SnapshotError("the build produced no divisor events; the day screen "
                            "exists to explain them, so an empty audit trail is a bug")

    state = _final_state_file(result, index_id)
    constituents = _constituents(weights, state.constituents)

    out_dir.mkdir(parents=True, exist_ok=True)
    # A rerun writes in place, so drop the previous run's completeness signal before
    # touching anything else. Without this, a rerun that dies halfway leaves the old
    # manifest.json sitting over a directory holding days.parquet from this build and
    # overview.json from the last one - a mixed snapshot that passes every existence
    # check and gets served.
    (out_dir / "manifest.json").unlink(missing_ok=True)

    _days_frame(levels, audit, result.history.reviews).to_parquet(
        out_dir / "days.parquet", index=False)
    _dated(audit).to_parquet(out_dir / "divisor_audit.parquet", index=False)
    _reviews_frame(result.history.reviews).to_parquet(
        out_dir / "reviews.parquet", index=False)

    context = baseline_from_build(result)
    context.save(out_dir / "chaos_baseline")
    _write_json(out_dir / "chaos_precomputed.json", _chaos_payload(context))

    _write_json(out_dir / "golden_diff.json", _golden_payload(levels))
    _write_json(out_dir / "evals.json", _evals_payload(corpus, eval_cases))

    _write_json(out_dir / "overview.json", _overview(result.history, index_id))
    _write_json(out_dir / "constituents.json",
                {"as_of": state.as_of, "constituents": constituents})
    _write_json(out_dir / "capacity.json", _capacity(constituents))
    _write_json(out_dir / "risk_attribution.json", {
        "risk": _parse_onepager(onepagers["risk"]),
        "attribution": _parse_onepager(onepagers["attribution"]),
    })

    manifest = _manifest(out_dir, spec, index_id, time.perf_counter() - started)
    _write_json(out_dir / "manifest.json", manifest.to_dict())
    _assert_complete(out_dir)
    return manifest


# --------------------------------------------------------------------------------------
# Inputs that live outside the build
# --------------------------------------------------------------------------------------


def _read_onepagers(directory: Path = ARTEFACTS_DIR) -> dict[str, str]:
    """The generated risk and attribution one-pagers, as markdown text."""
    out: dict[str, str] = {}
    for key, name in (("risk", "risk_onepager.md"),
                      ("attribution", "attribution_onepager.md")):
        path = directory / name
        if not path.exists():
            raise FileNotFoundError(
                f"{name} is missing from {directory}. The desk renders the parsed "
                "one-pagers on its index screen; regenerate them with "
                "miniftse.reporting.analytics before building a snapshot."
            )
        out[key] = path.read_text(encoding="utf-8")
    return out


def _read_eval_set(path: Path = EVAL_SET_PATH) -> list[EvalCase]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing. The evals screen reports the committed eval set; "
            "regenerate it with miniftse.agents.evals.save_eval_set."
        )
    cases = load_eval_set(path)
    if not cases:
        raise SnapshotError(f"{path} contains no evaluation cases")
    return cases


def _corpus_directories() -> tuple[Path, ...]:
    """The documents the methodology assistant answers from, as Task 8 builds it."""
    directories = (GROUND_RULES_DIR, MEMOS_DIR)
    for directory in directories:
        if not directory.is_dir():
            raise FileNotFoundError(
                f"{directory} is missing; the methodology assistant has no corpus "
                "to answer from."
            )
    return directories


# --------------------------------------------------------------------------------------
# Screen 1: the day view
# --------------------------------------------------------------------------------------


def _dated(frame: pd.DataFrame) -> pd.DataFrame:
    """A copy with `date` as datetime64, which is what parquet round-trips cleanly."""
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"])
    return out


def _days_frame(levels: pd.DataFrame, audit: pd.DataFrame,
                reviews: pd.DataFrame) -> pd.DataFrame:
    """One row per session: the published levels, what the divisor did, and whether the
    day was a review effective date.

    The per-date aggregate is a count, a first, a last, a max and a sum over columns the
    corporate action engine already computed. Nothing here recalculates an index figure -
    the individual events keep their own file (`divisor_audit.parquet`) so the day screen
    shows the detail rather than a rederivation of it. `divisor_before` is deliberately
    the opening divisor rather than a summed percentage change: divisor moves compound
    and adding them would be quietly wrong on a day with several events.
    """
    days = _dated(levels)
    events = _dated(audit)
    events["abs_continuity_error_bps"] = events["continuity_error_bps"].abs()

    per_date = events.groupby("date", as_index=False).agg(
        n_divisor_events=("event_id", "size"),
        divisor_before=("divisor_before", "first"),
        divisor_after=("divisor_after", "last"),
        worst_continuity_error_bps=("abs_continuity_error_bps", "max"),
        realised_return_bps=("realised_return_bps", "sum"),
        event_types=("event_type", lambda s: ", ".join(sorted(set(s.astype(str))))),
    )

    days = days.merge(per_date, on="date", how="left")
    days["n_divisor_events"] = days["n_divisor_events"].fillna(0).astype("int64")
    # A day with no event opens and closes on the published divisor.
    for column in ("divisor_before", "divisor_after"):
        days[column] = days[column].astype("float64").fillna(days["divisor"])
    for column in ("worst_continuity_error_bps", "realised_return_bps"):
        days[column] = days[column].astype("float64").fillna(0.0)
    days["event_types"] = days["event_types"].astype("object").fillna("")

    review_dates = set(_dated(reviews)["date"]) if not reviews.empty else set()
    days["is_review"] = days["date"].isin(review_dates)
    return days


def _reviews_frame(reviews: pd.DataFrame) -> pd.DataFrame:
    if reviews.empty:
        return pd.DataFrame({c: pd.Series(dtype="object") for c in REVIEW_COLUMNS})
    return _dated(reviews)


# --------------------------------------------------------------------------------------
# Screen 2: the chaos drill
# --------------------------------------------------------------------------------------


def _chaos_payload(context: ValidationContext) -> dict[str, Any]:
    frame, gaps = run_chaos_drill(context, seed=DRILL_SEED)
    if frame.empty:
        raise SnapshotError("the chaos drill returned no results")
    return {
        "drill": _records(frame),
        "gaps": gaps,
        "seed": DRILL_SEED,
        "summary": drill_summary(frame),
    }


# --------------------------------------------------------------------------------------
# Screen 4: reproducibility
# --------------------------------------------------------------------------------------


def _golden_payload(levels: pd.DataFrame, directory: Path = GOLDEN_DIR,
                    name: str = GOLDEN_NAME) -> dict[str, Any]:
    """The pinned master against this build. An unpinned master is a state the screen
    renders, not an error - a repository can legitimately have none yet."""
    if not (directory / f"{name}.json").exists():
        return {"pinned": False}

    master = GoldenMaster.load(directory, name)
    comparison = compare(master, levels)
    return {
        "pinned": True,
        "master": {
            "name": master.name,
            "git_sha": master.git_sha,
            "created_at": master.created_at,
            "content_hash": master.content_hash,
            "tolerance_bps": master.tolerance_bps,
            "n_rows": len(master.levels),
            "metrics": master.metrics,
        },
        "comparison": asdict(comparison),
        "report": comparison.report(),
    }


# --------------------------------------------------------------------------------------
# Screen 3: the eval report
# --------------------------------------------------------------------------------------


def _evals_payload(corpus: tuple[Path, ...], cases: list[EvalCase]) -> dict[str, Any]:
    """The eval report over the same assistant the site serves - ground rules plus
    memos, offline client. Showing the failures is the point of the screen."""
    assistant = MethodologyAssistant()
    for directory in corpus:
        assistant.add_directory(directory)

    report = run_evals(assistant, cases)
    return {
        "cases": _records(report.to_frame()),
        "metrics": {
            "accuracy": report.accuracy,
            "citation_precision": report.citation_precision,
            "abstention_accuracy": report.abstention_accuracy,
            "hallucination_rate": report.hallucination_rate,
        },
        "headline": report.headline(),
        "by_category": _records(report.by_category()),
        "failures": [
            {
                "case_id": r.case.case_id,
                "category": r.case.category,
                "question": r.case.question,
                "explanation": r.explain(),
            }
            for r in report.failures()
        ],
        "assistant_stats": report.assistant_stats,
        "corpus": [d.name for d in corpus],
    }


# --------------------------------------------------------------------------------------
# Screen 5: the four capacity-viz JSON files
#
# These four schemas belong to `docs/superpowers/specs/2026-08-11-capacity-viz-design.md`
# and are specified function-by-function in that plan's Tasks 5-7 (`build_overview`,
# `build_constituents`, `build_capacity`, `parse_onepager` in `viz/export.py`). That file
# does not exist yet. When it lands, these four helpers must be deleted and its functions
# called instead - two implementations of one schema will drift, and the front end
# renders whichever it is given. Do not fork these shapes.
#
# **Deliberate deviation, and the one thing a future `viz/export.py` must not "fix".**
# The JSON *shape* follows the capacity-viz spec exactly, but the four `stats` *values*
# are the library's published definitions, not the spec listing's inline arithmetic. The
# spec's version annualises over 252 trading days where `IndexHistory.annualised_return`
# uses calendar years, and counts days-the-divisor-moved where `IndexHistory.summary()`
# and the day screen's event table both say `len(divisor_audit)`. Keeping the inline
# version would put two different answers to the same question on one site - the overview
# tile and the factsheet disagreeing about the index's return, the overview tile and
# Screen 1 disagreeing about how many corporate actions there were. One source of truth
# per published figure; the library owns the definition.
# --------------------------------------------------------------------------------------


def _overview(history: IndexHistory, index_id: str) -> dict[str, Any]:
    """The level history and its headline statistics.

    The series pass straight through from `{index_id}_levels.parquet`'s shape. Every
    statistic is a library call - see the deviation note above.
    """
    df = history.levels.sort_values("date").reset_index(drop=True)

    return {
        "index_id": index_id,
        "dates": df["date"].astype(str).tolist(),
        "pr": df["price_return"].tolist(),
        "gtr": df["gross_total_return"].tolist(),
        "ntr": df["net_total_return"].tolist(),
        "stats": {
            # `gross_total_return` is the series the overview tiles describe, and it is
            # the default every one of these methods already uses.
            "annualised_return": history.annualised_return(),
            "annualised_vol": history.annualised_vol(),
            "max_drawdown": history.max_drawdown(),
            "divisor_events": len(history.divisor_audit),
        },
    }


def _constituents(weights: pd.DataFrame, state: dict[str, Any]) -> list[dict[str, Any]]:
    """`weights`: date, security_id, weight. `state`: the constituent sub-dict of an
    `IndexStateFile`, keyed by security id and carrying adv/icb_industry/country/
    size_band."""
    latest_date = weights["date"].max()
    latest = weights[weights["date"] == latest_date]

    rows: list[dict[str, Any]] = []
    for _, row in latest.iterrows():
        sid = str(row["security_id"])
        meta = state.get(sid, {})
        rows.append({
            "security_id": sid,
            "weight": float(row["weight"]),
            "adv": float(meta.get("adv", 0.0)),
            "sector": meta.get("icb_industry", ""),
            "country": meta.get("country", ""),
            "size_band": meta.get("size_band", ""),
        })
    rows.sort(key=lambda r: r["weight"], reverse=True)
    return rows


def _capacity(constituents: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemes": SCHEME_PROPERTIES,
        "constituents": constituents,
        "capacity_params": {"participation": 0.20, "max_days_to_trade": 5.0},
    }


def _parse_onepager(text: str) -> dict[str, Any]:
    """Parse a generated one-pager into JSON without re-running the analysis that
    produced it. Each file is a title line, a bold metadata line, then a series of
    `## Heading` sections holding prose and/or a markdown table."""
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("# ") else ""

    meta = ""
    for line in lines[1:6]:
        if line.strip().startswith("**"):
            meta = line.strip().replace("**", "")
            break

    section_parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)[1:]
    sections: list[dict[str, Any]] = []
    for i in range(0, len(section_parts), 2):
        heading = section_parts[i].strip()
        body_lines = section_parts[i + 1].splitlines()

        table_lines = [
            ln for ln in body_lines
            if ln.strip().startswith("|") and ln.strip().endswith("|")
        ]
        table = None
        if len(table_lines) >= 2:
            columns = [c.strip() for c in table_lines[0].strip("|").split("|")]
            rows = [
                [c.strip() for c in ln.strip("|").split("|")]
                for ln in table_lines[2:]  # skip header row + "---" separator row
            ]
            table = {"columns": columns, "rows": rows}

        prose_lines = [
            ln for ln in body_lines
            if ln.strip() and not ln.strip().startswith("|")
            and not ln.strip().startswith("---")
        ]
        text_body = " ".join(prose_lines).replace("**", "")

        sections.append({"heading": heading, "text": text_body, "table": table})

    return {"title": title, "meta": meta, "sections": sections}


# --------------------------------------------------------------------------------------
# Manifest and plumbing
# --------------------------------------------------------------------------------------


def _final_state_file(result: BuildResult, index_id: str) -> IndexStateFile:
    """The closing constituent set, in the same shape `{index_id}_state.json` uses, so
    the index screen reads one format whether it came from a snapshot or a daily run."""
    state = result.calculator.final_state
    if state is None:
        raise SnapshotError("the build left no final state; the index screen needs the "
                            "closing constituent set")
    last = result.history.levels.iloc[-1]
    return IndexStateFile.from_state(
        index_id, state,
        pr=float(last["price_return"]),
        gtr=float(last["gross_total_return"]),
        ntr=float(last["net_total_return"]),
    )


def _manifest(out_dir: Path, spec: BuildSpec, index_id: str,
              duration: float) -> SnapshotManifest:
    sha, dirty = git_sha(REPO_ROOT)
    return SnapshotManifest(
        git_sha=sha,
        git_dirty=dirty,
        created_at=dt.datetime.now(dt.UTC).isoformat(),
        index_id=index_id,
        build_spec={
            "index_id": str(spec.index_config.index_id),
            "n_securities": spec.universe_config.n_securities,
            "seed": spec.universe_config.seed,
            "universe_fingerprint": spec.universe_config.fingerprint(),
            "start": spec.start.isoformat(),
            "end": spec.end.isoformat(),
        },
        duration_seconds=round(duration, 1),
        files=_file_hashes(out_dir),
    )


def _file_hashes(out_dir: Path) -> dict[str, str]:
    """sha256 of every file written, so a tampered or truncated snapshot is detectable.
    `manifest.json` is excluded - it is the thing being written."""
    return {
        path.relative_to(out_dir).as_posix():
            hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(out_dir.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }


def _assert_complete(out_dir: Path) -> None:
    missing = [name for name in EXPECTED_FILES if not (out_dir / name).exists()]
    if missing:
        raise SnapshotError(
            f"snapshot in {out_dir} is incomplete: {', '.join(missing)}"
        )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(_jsonable(payload), indent=2), encoding="utf-8")


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [_jsonable(row) for row in frame.to_dict("records")]


def _jsonable(obj: Any) -> Any:
    """numpy scalars, dates and non-finite floats into things `json.dumps` accepts.

    `default=str` would turn a numpy bool into the string "True", which renders as a
    truthy value in a template whatever it actually was.
    """
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return _jsonable(obj.item())
    if isinstance(obj, bool | int | str) or obj is None:
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dt.date | dt.datetime | pd.Timestamp):
        return str(obj)
    return str(obj)
