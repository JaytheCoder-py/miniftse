"""Load a `desk/snapshot.py` output directory into memory once, at process startup.

The application never touches disk again after this runs: every route reads
`request.app.state.desk`, a frozen `DeskState` built exactly once by the `lifespan`
context manager in `app.py`. Loading is expected to take a fraction of a second - it is
JSON and parquet already computed, not anything recomputed - and `test_desk.py` asserts
the whole thing, including building the methodology assistant's index, stays under two
seconds.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from miniftse.agents.llm import AnthropicLlm, CachingLlm, LlmClient, OfflineLlm
from miniftse.agents.rag import MethodologyAssistant
from miniftse.desk.snapshot import EXPECTED_FILES, GROUND_RULES_DIR, MEMOS_DIR
from miniftse.quality.rules import ValidationContext


@dataclass(frozen=True)
class DeskState:
    """Everything a route needs to answer a request, read-only for the life of the
    process. One instance is built by `load_desk_state` in the app's `lifespan` and
    never rebuilt - there is no code path that mutates a field or reloads a file after
    startup, which is what makes "loaded once, served from memory" true rather than
    aspirational.
    """

    overview: dict[str, Any]
    days: pd.DataFrame
    divisor_audit: pd.DataFrame
    reviews: pd.DataFrame
    chaos_baseline: ValidationContext
    chaos_precomputed: dict[str, Any]
    golden_diff: dict[str, Any]
    evals: dict[str, Any]
    constituents: dict[str, Any]
    capacity: dict[str, Any]
    risk_attribution: dict[str, Any]
    manifest: dict[str, Any]
    assistant: MethodologyAssistant
    loaded_at: dt.datetime


def _select_llm() -> LlmClient:
    """The backend the deployed methodology assistant answers with, chosen by
    `MINIFTSE_LLM` so the choice is an environment variable, not a code change.

    Unset, `offline`, or anything this doesn't recognise all resolve to `OfflineLlm` -
    that is the deployed default, and it is deliberately also the fallback for a typo:
    a live site that quietly serves an offline assistant is recoverable, one that
    refuses to start because `MINIFTSE_LLM=Offline` doesn't match a literal is not.
    `anthropic` opts into a cached real model, wrapped so repeated identical questions
    (the three seeded example questions on `/ask`, most of all) cost one call rather
    than one per request.
    """
    choice = os.environ.get("MINIFTSE_LLM", "offline").strip().lower()
    if choice == "anthropic":
        return CachingLlm(AnthropicLlm(api_key=os.environ.get("ANTHROPIC_API_KEY")))
    return OfflineLlm()


def _build_assistant() -> MethodologyAssistant:
    """Mirrors `snapshot._evals_payload`'s construction - the same corpus directories,
    the same `add_directory` call - so the assistant behind `/ask` is not a second,
    silently different implementation from the one the eval report in `evals.json` was
    scored against. Only the LLM client differs, and that is the one axis meant to vary
    between an offline eval run and a deployed site.
    """
    for directory in (GROUND_RULES_DIR, MEMOS_DIR):
        if not directory.is_dir():
            raise FileNotFoundError(
                f"{directory} is missing; the methodology assistant has no corpus to answer from."
            )
    assistant = MethodologyAssistant(client=_select_llm())
    for directory in (GROUND_RULES_DIR, MEMOS_DIR):
        assistant.add_directory(directory)
    return assistant


def _read_json(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _load[T](data_dir: Path, name: str, loader: Callable[[Path], T]) -> T:
    """Load one snapshot file (or, for `chaos_baseline`, directory) with `loader`,
    turning any failure into a `RuntimeError` that names the file and the fix.

    A present-but-corrupt file - truncated parquet, hand-edited JSON with a stray
    comma - would otherwise fail startup with whatever bare exception
    `json.loads`/`pandas.read_parquet`/`ValidationContext.load` happens to raise:
    `json.JSONDecodeError: Expecting value: line 1 column 1` names a line and column in
    no file in particular, and a Space's boot log is not somewhere anyone goes hunting
    through a dozen candidate files to find the one that broke. `raise ... from exc`
    keeps the original exception attached as `__cause__` - nothing here hides the real
    error, this only adds the context a bare re-raise would leave out.
    """
    path = data_dir / name
    try:
        return loader(path)
    except Exception as exc:
        raise RuntimeError(
            f"{name} in {data_dir} is corrupt or unreadable ({exc!r}). "
            "Rebuild the snapshot with `make desk-data`."
        ) from exc


def load_desk_state(data_dir: Path) -> DeskState:
    """Load every artefact `desk/snapshot.py` writes into a `DeskState`.

    Validates the directory against `snapshot.EXPECTED_FILES` - the same tuple the
    snapshot writer checks its own output against in `_assert_complete` - so the two
    ends of the pipeline can never define "a complete snapshot" differently. Missing
    anything raises `FileNotFoundError` naming the file and the fix, rather than
    starting the server to serve four screens and 404 the fifth. A file that exists but
    fails to parse raises through `_load` instead, for the same reason - see its
    docstring.
    """
    data_dir = Path(data_dir)
    for name in EXPECTED_FILES:
        if not (data_dir / name).exists():
            raise FileNotFoundError(f"{name} missing from {data_dir} — run `make desk-data`")

    return DeskState(
        overview=_load(data_dir, "overview.json", _read_json),
        days=_load(data_dir, "days.parquet", pd.read_parquet),
        divisor_audit=_load(data_dir, "divisor_audit.parquet", pd.read_parquet),
        reviews=_load(data_dir, "reviews.parquet", pd.read_parquet),
        chaos_baseline=_load(data_dir, "chaos_baseline", ValidationContext.load),
        chaos_precomputed=_load(data_dir, "chaos_precomputed.json", _read_json),
        golden_diff=_load(data_dir, "golden_diff.json", _read_json),
        evals=_load(data_dir, "evals.json", _read_json),
        constituents=_load(data_dir, "constituents.json", _read_json),
        capacity=_load(data_dir, "capacity.json", _read_json),
        risk_attribution=_load(data_dir, "risk_attribution.json", _read_json),
        manifest=_load(data_dir, "manifest.json", _read_json),
        assistant=_build_assistant(),
        loaded_at=dt.datetime.now(dt.UTC),
    )
