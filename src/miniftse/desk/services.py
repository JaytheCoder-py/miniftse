"""Desk service layer: pure-pandas functions over a `DeskState`'s precomputed frames.

No I/O and no FastAPI import lives here - every function takes a `DeskState` already
loaded into memory and returns plain dataclasses/dicts a route can render. No index
mathematics either: every bps figure this module reports is either read straight off a
column `desk/snapshot.py` already wrote (`continuity_error_bps`, `realised_return_bps`,
`worst_continuity_error_bps`, `one_way_turnover`, ...) or, for the one figure with no
library equivalent - a day's total headline move - derived by the single small helper
at the bottom of this file, `_to_bps`.

`explain_day`'s `narrative` is assembled by plain string formatting over numbers this
module already computed - **not by a language model**. This is exactly the kind of
client-facing output `agents/llm.py`'s first rule governs ("no number in a client-facing
output may originate from a language model"), and the cheapest way to comply with that
rule here is to not involve a model at all: there is nothing about "why did the level
move" that formatting a handful of already-correct numbers doesn't answer.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pandas as pd

from miniftse.agents.drafter import (
    ClientResponseDrafter,
    DraftResponse,
    FactPack,
    NumberGuard,
    performance_facts,
)
from miniftse.agents.llm import OfflineLlm
from miniftse.agents.rag import RetrievedAnswer
from miniftse.corpactions.events import apply_order
from miniftse.desk.state import DeskState
from miniftse.quality.faults import FAULTS, Fault, run_chaos_drill


@dataclass(frozen=True)
class DayExplanation:
    """Everything the day screen (Task 5) needs to explain one session.

    `levels` is keyed by the same three series names `days.parquet` already uses
    (`price_return`, `gross_total_return`, `net_total_return`), each mapping to
    `{"open": ..., "close": ...}`. "Open" is the prior session's close - this index
    publishes one level per day, not an intraday print, so there is no other honest
    reading of "open" available from the data.
    """

    levels: dict[str, dict[str, float]]
    divisor_before: float
    divisor_after: float
    events: list[dict[str, Any]]
    review: dict[str, Any] | None
    market_move_bps: float
    structural_move_bps: float
    narrative: str


def explain_day(state: DeskState, date: dt.date) -> DayExplanation:
    """Assemble the day screen's content for one session, from precomputed frames only.

    Raises `KeyError` for any date the snapshot never published a level for - a
    silently empty explanation would be worse than an error the route layer (Task 5)
    can turn into a 400.
    """
    target = pd.Timestamp(date)
    days = state.days.sort_values("date").reset_index(drop=True)
    matches = days.index[days["date"] == target]
    if len(matches) == 0:
        raise KeyError(f"{date} is not a date this index published a level for")

    i = int(matches[0])
    today = _row(days, i)
    # The prior session's close stands in for "today's open" - see `DayExplanation`.
    # On the index's very first published date there is no prior session; treat that
    # as a zero-move day rather than fabricating one.
    prior = _row(days, i - 1) if i > 0 else today

    open_pr = float(prior["price_return"])
    close_pr = float(today["price_return"])
    total_move_bps = _to_bps(close_pr / open_pr - 1.0) if open_pr else 0.0

    # A genuine, already-scaled bps figure straight off the audit trail's aggregate -
    # not derived here. Zero on a day with no divisor events (`_days_frame` fills it).
    structural_move_bps = float(today["realised_return_bps"])
    market_move_bps = total_move_bps - structural_move_bps

    events = _event_dicts(state.divisor_audit, target)
    review = _review_dict(state.reviews, target)

    levels = {
        "price_return": {"open": open_pr, "close": close_pr},
        "gross_total_return": {
            "open": float(prior["gross_total_return"]),
            "close": float(today["gross_total_return"]),
        },
        "net_total_return": {
            "open": float(prior["net_total_return"]),
            "close": float(today["net_total_return"]),
        },
    }

    narrative = _narrative(
        date=date,
        close_pr=close_pr,
        total_move_bps=total_move_bps,
        events=events,
        review=review,
        market_move_bps=market_move_bps,
        structural_move_bps=structural_move_bps,
        worst_continuity_error_bps=float(today["worst_continuity_error_bps"]),
    )

    return DayExplanation(
        levels=levels,
        divisor_before=float(today["divisor_before"]),
        divisor_after=float(today["divisor_after"]),
        events=events,
        review=review,
        market_move_bps=market_move_bps,
        structural_move_bps=structural_move_bps,
        narrative=narrative,
    )


def available_dates(state: DeskState) -> list[dt.date]:
    """Every date this index published a level for, ascending.

    This is the closed set `/day` (Task 5) validates a requested date against before
    calling `explain_day` at all - kept here, beside the frame it reads, rather than
    the route re-deriving it from `state.days` itself, per the module docstring's rule
    that no retrieval logic lives in the route layer.
    """
    return [_as_date(value) for value in state.days["date"].sort_values()]


def notable_days(state: DeskState) -> list[dict[str, Any]]:
    """Four pinned dropdown entries: the largest divisor event, the largest review
    turnover, the largest continuity error, and the largest single-day move.

    Each is an existing column's `idxmax`, not a recomputation of any figure - the one
    exception is the day-move ranking, which reuses the same `_to_bps` helper
    `explain_day` uses, for the same reason (no precomputed column holds it).
    """
    notable: list[dict[str, Any]] = []

    audit = state.divisor_audit
    corp_events = audit.loc[audit["event_type"] != "REVIEW"]
    if not corp_events.empty:
        row = _row(corp_events, corp_events["divisor_change_pct"].abs().idxmax())
        notable.append({
            "category": "largest_divisor_event",
            "date": _as_date(row["date"]),
            "reason": (
                f"{row['event_type']} on {row['security_id']} moved the divisor "
                f"{float(row['divisor_change_pct']):+.2%}."
            ),
            "value": float(row["divisor_change_pct"]),
        })

    reviews = state.reviews
    if not reviews.empty:
        row = _row(reviews, reviews["one_way_turnover"].idxmax())
        notable.append({
            "category": "largest_review_turnover",
            "date": _as_date(row["date"]),
            "reason": (
                f"the periodic review turned over "
                f"{float(row['one_way_turnover']):.2%} of index weight one-way."
            ),
            "value": float(row["one_way_turnover"]),
        })

    if not audit.empty:
        row = _row(audit, audit["continuity_error_bps"].abs().idxmax())
        notable.append({
            "category": "largest_continuity_error",
            "date": _as_date(row["date"]),
            "reason": (
                f"{row['event_type']} on {row['security_id']} carried a continuity "
                f"error of {float(row['continuity_error_bps']):+.2f} bps."
            ),
            "value": float(row["continuity_error_bps"]),
        })

    days = state.days.sort_values("date").reset_index(drop=True)
    if len(days) > 1:
        moves = days["price_return"].pct_change().map(_to_bps)
        idx = moves.abs().idxmax()
        row = _row(days, idx)
        notable.append({
            "category": "largest_single_day_move",
            "date": _as_date(row["date"]),
            "reason": (
                f"the price-return level moved {float(moves.loc[idx]):+.1f} bps in "
                "one session."
            ),
            "value": float(moves.loc[idx]),
        })

    return notable


def ask(state: DeskState, question: str) -> RetrievedAnswer:
    """The methodology assistant's answer to `question`.

    A pure delegation to `state.assistant.ask` - the retrieval, scope-checking and
    citation logic all live in `agents/rag.py`'s `MethodologyAssistant`, built once in
    `desk/state.py`'s `load_desk_state`. This function exists only so a route (Task 9)
    depends on the desk service layer rather than reaching into `agents/` directly,
    the same boundary every other function in this module keeps for its own domain.
    """
    return state.assistant.ask(question)


# --------------------------------------------------------------------------------------
# Screen 3c: the draft tab
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftQuestion:
    """One canned client question `/draft` (Task 10) can compose a response to.

    `question_id` is this question's own index into `DRAFT_QUESTIONS` - the closed set
    `/draft/render` validates a posted `question_id` against, the same pattern
    `_VALID_FAULT_IDS` gives `/chaos/run`'s `fault_id`.

    All three canned questions are fed by `agents/drafter.py`'s `performance_facts`,
    not by `attribution_facts`, `turnover_facts` or `tracking_difference_facts` - a
    deliberate choice, not an oversight. `DeskState` carries no `IndexHistory` (no
    `BrinsonFachler` attribution, no `ReviewEngine` with its per-review capped-weight
    snapshots, no external fund-return figure) - only what `desk/snapshot.py` already
    precomputed into `overview.json` and `days.parquet`. `performance_facts` is the one
    builder whose entire input - the level history, the annualised return/vol/drawdown -
    is already sitting in `DeskState`, honestly, with no adapter standing in for data
    the desk was never given. Forcing the other three builders to run here would mean
    either rebuilding an `IndexHistory` from scratch (exactly the "recompute ten years
    of history" cold start `desk/snapshot.py`'s module docstring exists to avoid) or
    inventing numbers to fill gaps the snapshot has no honest answer for - the opposite
    of what this screen exists to demonstrate.

    `period_days` varies the horizon `performance_facts` reports over, which is what
    gives the three questions genuinely different fact packs rather than three copies of
    the same one. `guidance` is passed straight to `ClientResponseDrafter.draft` -
    inert under the offline template `_offline_fact_restatement` below (it ignores the
    prompt entirely), kept for when a real backend (`MINIFTSE_LLM=anthropic`) drafts
    instead.
    """

    question_id: int
    text: str
    period_days: int
    guidance: str


DRAFT_QUESTIONS: tuple[DraftQuestion, ...] = (
    DraftQuestion(
        question_id=0,
        text="How has the index performed over the past month?",
        period_days=21,
        guidance="A short, one-paragraph update. Lead with the headline move.",
    ),
    DraftQuestion(
        question_id=1,
        text="How has the index performed over the past year?",
        period_days=252,
        guidance="A trustee-level annual performance update.",
    ),
    DraftQuestion(
        question_id=2,
        text="How has the index performed since inception, and how volatile has it been?",
        # Larger than any real snapshot's session count, so `performance_facts` reports
        # only the since-inception figures (annualised return/vol/drawdown) and skips
        # the "last N sessions" period comparison entirely - the honest reading of
        # "since inception" for a builder whose period comparison is always relative to
        # a fixed lookback.
        period_days=1_000_000,
        guidance="Cover the full track record: annualised return, volatility, and the "
                 "worst drawdown. No speculation about what comes next.",
    ),
)
"""The hard-coded, closed set `/draft` and `/draft/render` (Task 10) validate a
`question_id` against - see `DraftQuestion`'s docstring for why every one of them is fed
by `performance_facts`."""


def draft_questions() -> tuple[DraftQuestion, ...]:
    """The picker `/draft` (Task 10) renders."""
    return DRAFT_QUESTIONS


@dataclass(frozen=True)
class DraftOutcome:
    """One canned question's drafted response, ready for the `/draft` screen.

    `response` is `agents/drafter.py`'s own `DraftResponse` - its `.draft` is exactly
    the text `response.guard` was checked against (the base draft, plus the
    demonstration sentence when `inject_bad_number` was set), so the two never
    disagree about what was verified. `base_draft` is the drafted text *before* that
    sentence was appended, kept separately only so the template can highlight the
    injected sentence on its own rather than re-deriving it by slicing `response.draft`.
    `injected_sentence` is `None` whenever `inject_bad_number` was false.
    """

    question: DraftQuestion
    response: DraftResponse
    base_draft: str
    injected_sentence: str | None


def render_draft(state: DeskState, question_id: int, inject_bad_number: bool) -> DraftOutcome:
    """Compose one canned question's fact pack, draft a response, and run the guard.

    Raises `IndexError` for a `question_id` outside `DRAFT_QUESTIONS` - the route layer
    (Task 10) turns that into a 400, the same pattern `explain_day`/`run_drill` use for
    an out-of-range date/`fault_id`.

    Nothing here computes a fact, drafts prose, or decides pass/fail - it composes three
    calls that already live in `agents/drafter.py`: `performance_facts` builds the pack
    from `_history_adapter`'s view of the snapshot, `ClientResponseDrafter.draft` writes
    the prose and runs `NumberGuard.check` once, and - only when `inject_bad_number` is
    set - a plausible, deliberately-unaccounted-for sentence is appended to that draft
    and the *same* `NumberGuard.check` is asked to check it again. The guard's own
    verification is what rejects the injected sentence (see
    `_fabricate_unverified_sentence`, which only accepts a candidate number once it has
    confirmed the guard does not already recognise it); this function does not decide
    that outcome itself.
    """
    if not (0 <= question_id < len(DRAFT_QUESTIONS)):
        raise IndexError(
            f"question_id {question_id} is out of range "
            f"(0..{len(DRAFT_QUESTIONS) - 1})"
        )
    question = DRAFT_QUESTIONS[question_id]
    pack = performance_facts(_history_adapter(state), period_days=question.period_days)

    drafter = ClientResponseDrafter(client=_offline_drafting_client(pack, question.text))
    response = drafter.draft(question.text, pack, guidance=question.guidance)
    base_draft = response.draft

    injected_sentence: str | None = None
    if inject_bad_number:
        injected_sentence = _fabricate_unverified_sentence(pack)
        full_text = base_draft + injected_sentence
        response = replace(response, draft=full_text, guard=NumberGuard.check(full_text, pack))

    return DraftOutcome(
        question=question, response=response,
        base_draft=base_draft, injected_sentence=injected_sentence,
    )


def _history_adapter(state: DeskState) -> Any:
    """Adapts what `desk/snapshot.py` already computed into the slice of
    `IndexHistory`'s interface `agents.drafter.performance_facts` reads (`.levels`,
    `.config.name`, `.annualised_return()`, `.annualised_vol()`, `.max_drawdown()`).

    Not a second implementation of any of those figures: `levels` is `state.days`
    (which carries `date`/`price_return`/`gross_total_return`/`net_total_return`,
    exactly the columns `performance_facts` reads, sorted the same way `explain_day`
    sorts them), and the three stats are read straight off `state.overview["stats"]` -
    the same numbers `_overview` in `desk/snapshot.py` wrote from `IndexHistory` calls
    at build time. `DeskState` does not carry an `IndexHistory` itself - rebuilding one
    here would mean replaying the whole reference build the snapshot exists to avoid -
    so this is the honest way to feed `performance_facts` from what the desk actually
    has loaded in memory.
    """
    stats = state.overview["stats"]
    levels = state.days.sort_values("date").reset_index(drop=True)
    return SimpleNamespace(
        levels=levels,
        config=SimpleNamespace(name=state.overview["index_id"]),
        annualised_return=lambda: float(stats["annualised_return"]),
        annualised_vol=lambda: float(stats["annualised_vol"]),
        max_drawdown=lambda: float(stats["max_drawdown"]),
    )


def _offline_drafting_client(pack: FactPack, question: str) -> OfflineLlm:
    """An `OfflineLlm` that can actually draft from a fact-pack prompt.

    `OfflineLlm._default` (`agents/llm.py`) only recognises the `<context>...</context>`
    shape `agents/rag.py`'s retrieval prompts build; `ClientResponseDrafter.draft`'s
    prompt (`FactPack.render()` plus the question) has no such tag, so without a handler
    here the deployed offline default would draft the same generic refusal
    ("I cannot answer that from the methodology documents available...") for every
    canned question, whatever the fact pack held - a demo that shows a real fact table
    next to prose that ignores it entirely.

    The handler below is a template only, in the same spirit as `_default`'s own
    "restate the supplied evidence rather than inventing anything": it rearranges
    `Fact` values the caller already computed into prose and invents nothing - no number
    is computed or verified here, both stay in `NumberGuard` and the fact-pack builders.
    `OfflineLlm.handlers` is itself the extension point for exactly this kind of
    composition, not a new one introduced by the desk.

    Deliberately quotes each `Fact`'s `key`/`formatted` value only - never
    `fact.line()` (which also carries `description`/`source`), and never
    `pack.context`. Two reasons, both the same shape: `FactPack.allowed_numbers()`
    only ever scans a fact's *value* (in several roundings, signed and unsigned) and
    `pack.context`'s raw text, never a fact's free-text `description` -
    `performance_facts` writes descriptions like "price return over the last 21
    sessions", and 21 (`period_days`) is not a fact value anywhere in the pack, so
    echoing descriptions verbatim would have `NumberGuard` block its own draft on a
    number that was only ever English, not a claim. And `performance_facts`'s
    `as_of` context text is an ISO-style date string ("2019-12-30 00:00:00"): the same
    naive `_NUMBER` regex `allowed_numbers()` scans it with reads "-12-30" as containing
    the *negative* numbers -12 and -30 (a dash read as a minus sign), while
    `NumberGuard.check` strips leading signs when extracting numbers from the draft
    itself - so a draft that quoted this string back would compare a stripped "30"
    against an allowed set that only ever recorded the signed "-30", and lose. Both are
    pre-existing properties of `agents/drafter.py`, not bugs introduced here; the fix on
    this side is simply not to build a demo draft that walks into either edge case. The
    full fact table (description, source and context included) is still shown
    separately in `partials/draft_result.html`, straight from `outcome.response.
    fact_pack` - that render path never goes through the guard.
    """

    def _restate_facts(prompt: str, system: str | None) -> str:
        del prompt, system
        lines = [f'In response to your question, "{question}":', ""]
        lines += [
            f"- {fact.key.replace('_', ' ')}: {fact.formatted}"
            for fact in pack.facts.values()
        ]
        lines += [
            "",
            "Every figure above comes from the index's computed history; nothing has "
            "been estimated, projected, or supplied by a model.",
        ]
        return "\n".join(lines)

    return OfflineLlm(handlers={".*": _restate_facts})


def _fabricate_unverified_sentence(pack: FactPack) -> str:
    """A plausible-looking client sentence carrying one figure guaranteed absent from
    `pack`'s allowed numbers - the demonstration `inject_bad_number` (Task 10) exists
    for.

    The candidate figure is only accepted once it has been confirmed missing from
    exactly the set `NumberGuard.check` itself consults (`pack.allowed_numbers() |
    NumberGuard.STRUCTURAL`) - so it is the guard's own verification that rejects this
    sentence when `render_draft` appends it and checks again, not any pass/fail
    decision made here.
    """
    allowed = pack.allowed_numbers() | NumberGuard.STRUCTURAL
    candidate = 13.37
    while f"{candidate:.2f}" in allowed:
        candidate += 1.0
    return (
        f" A preliminary internal estimate also suggests the index could return "
        f"{candidate:.2f}% next quarter, though this figure has not been through the "
        "standard review process."
    )


_FAULTS_BY_ID: dict[str, Fault] = {fault.fault_id: fault for fault in FAULTS}
"""Every chaos-drill fault, keyed by id, built once at import time. `run_drill` filters
`FAULTS` down to a single fault through this lookup rather than scanning the tuple per
request - and a missing key is exactly the `KeyError` an unrecognised `fault_id` should
raise, the same pattern `explain_day` uses for an out-of-range date."""


@dataclass(frozen=True)
class DrillOutcome:
    """One fault's live chaos-drill result, run against `state.chaos_baseline`.

    Field names mostly follow `faults.DrillResult` - `severity` is that dataclass's
    `highest_severity`, `publication_blocked` is its `blocked_publication`, renamed
    because a desk route talks about "publication", not "blocked", and because
    "highest" only matters when several rules could fire; here exactly one fault is in
    play. `realism` is not on `DrillResult` at all - it lives on `Fault` (why the
    defect is worth drilling for) and is looked up alongside it. `coverage_gap` is the
    one sentence from `run_chaos_drill`'s gap list that concerns this fault, or `None`
    if it was caught by the rule meant to catch it, or if it could not be injected into
    this cross-section at all.

    `fault_name` and `detail` (Task 7 widening): copied straight off the same drill row
    `detected`/`expected_detector`/etc. already come from - no new computation, just two
    more fields the `/chaos/run` fragment wants for display (a human-readable name next
    to the id, and the one-line detail of what the injector actually did) that the
    original Task 6 shape happened not to need.
    """

    fault_id: str
    fault_name: str
    detected: bool
    detected_by: tuple[str, ...]
    expected_detector: str
    severity: str
    publication_blocked: bool
    realism: str
    detail: str
    coverage_gap: str | None


def run_drill(state: DeskState, fault_id: str, seed: int) -> DrillOutcome:
    """Re-run one chaos-drill fault live, against the app's shared baseline.

    Raises `KeyError` for a `fault_id` not in `FAULTS` - the route layer (Task 7) turns
    that into a 400.

    `state.chaos_baseline` is loaded once at startup and shared by every request this
    process ever serves; this function must never leave a mark on it. It doesn't, but
    not because of anything added here: every fault function in `quality.faults` makes
    its own working copy (`faults._copy`) of the context before mutating anything, so
    handing `state.chaos_baseline` straight to `run_chaos_drill` - rather than copying
    it again first - is deliberate, not an oversight.
    `test_run_drill_does_not_mutate_the_shared_baseline` is what stands behind that
    claim, not this comment.

    No drill or validation logic is reimplemented here: `run_chaos_drill` is the
    library's own entry point, filtered to a single fault through its own `faults`
    parameter, exactly as it is for the full battery `desk/snapshot.py` precomputes.
    """
    fault = _FAULTS_BY_ID[fault_id]
    frame, gaps = run_chaos_drill(state.chaos_baseline, seed=seed, faults=(fault,))
    row = frame.iloc[0]

    return DrillOutcome(
        fault_id=str(row["fault_id"]),
        fault_name=str(row["fault_name"]),
        detected=bool(row["detected"]),
        detected_by=_split_detected_by(row["detected_by"]),
        expected_detector=str(row["expected_detector"]),
        severity=str(row["highest_severity"]),
        publication_blocked=bool(row["blocked_publication"]),
        realism=fault.realism,
        detail=str(row["detail"]),
        coverage_gap=gaps[0] if gaps else None,
    )


def chaos_drill_rows(state: DeskState) -> list[dict[str, Any]]:
    """The 12 precomputed drill rows (`state.chaos_precomputed["drill"]`), each carrying
    its fault's `realism` string alongside the fields `desk/snapshot.py` already wrote
    (`fault_id`, `fault_name`, `detected`, `detected_by`, `expected_detector`,
    `caught_by_expected`, `highest_severity`, `blocked_publication`, `detail`).

    `chaos_precomputed.json` does not carry `realism` itself - see `DrillOutcome`'s
    docstring for why (it lives on `Fault`, not `DrillResult`). Joined here, once, so
    the `/chaos` GET route (Task 7) does not need a `FAULTS` lookup of its own - the
    module docstring's "no retrieval logic in the route" rule covers the GET handler
    the same way `run_drill` already covers the POST.
    """
    return [
        {**row, "realism": _FAULTS_BY_ID[str(row["fault_id"])].realism}
        for row in state.chaos_precomputed["drill"]
    ]


def precomputed_drill_row(state: DeskState, fault_id: str) -> dict[str, Any]:
    """The one precomputed drill row for `fault_id`, from
    `state.chaos_precomputed["drill"]`, with `realism` joined in the same way
    `chaos_drill_rows` does.

    Used by the `/chaos/run` POST route's timeout fallback (Task 7): if the live drill
    exceeds its time budget, the route falls back to what `desk/snapshot.py` already
    computed for this fault, rather than leaving the request to hang or return an error
    for something the desk already has an answer to.

    Raises `KeyError` for a `fault_id` outside the precomputed set - the route validates
    `fault_id` against `quality.faults.FAULTS` before ever calling this, so that should
    only happen if the two closed sets ever disagree, not on ordinary bad input.
    """
    for row in state.chaos_precomputed["drill"]:
        if str(row["fault_id"]) == fault_id:
            return {**row, "realism": _FAULTS_BY_ID[fault_id].realism}
    raise KeyError(fault_id)


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _event_dicts(divisor_audit: pd.DataFrame, date: pd.Timestamp) -> list[dict[str, Any]]:
    """One dict per corporate-action divisor event on `date`, in application order.

    Excludes the audit trail's synthetic ``"REVIEW"`` rows (`security_id == "*"`,
    appended by `IndexCalculator._apply_review`) - a periodic review is not a
    corporate action and has no entry in the apply-order table; it gets its own,
    richer record via `review` instead.
    """
    day_events = divisor_audit.loc[
        (divisor_audit["date"] == date) & (divisor_audit["event_type"] != "REVIEW")
    ].copy()
    if day_events.empty:
        return []
    day_events["apply_order"] = day_events["event_type"].map(apply_order)
    day_events = day_events.sort_values(["apply_order", "security_id"])
    return [
        {str(k): v for k, v in record.items()} for record in day_events.to_dict("records")
    ]


def _review_dict(reviews: pd.DataFrame, date: pd.Timestamp) -> dict[str, Any] | None:
    """The single review row effective on `date`, or `None` if this was not a review
    date. `reviews` carries at most one row per date - a review is a periodic, whole-
    index event, not something that happens twice in a day."""
    match = reviews.loc[reviews["date"] == date]
    if match.empty:
        return None
    return {str(k): v for k, v in match.iloc[0].to_dict().items()}


def _narrative(
    date: dt.date,
    close_pr: float,
    total_move_bps: float,
    events: list[dict[str, Any]],
    review: dict[str, Any] | None,
    market_move_bps: float,
    structural_move_bps: float,
    worst_continuity_error_bps: float,
) -> str:
    """Plain-English string formatting over numbers already computed above. See the
    module docstring: this is deliberately not a language model."""
    sentences = [
        f"On {date.isoformat()}, the price-return level closed at {close_pr:.2f}, "
        f"a move of {total_move_bps:+.1f} bps versus the prior close."
    ]

    if not events:
        sentences.append(
            "No divisor events were recorded on this date, so the entire move "
            f"reflects market price changes ({market_move_bps:+.1f} bps)."
        )
    else:
        kinds = ", ".join(sorted({str(event["event_type"]) for event in events}))
        plural = "event" if len(events) == 1 else "events"
        sentences.append(
            f"{len(events)} divisor {plural} were recorded ({kinds}), contributing "
            f"{structural_move_bps:+.1f} bps of realised structural return; the "
            f"market accounted for the remaining {market_move_bps:+.1f} bps. The "
            "worst continuity error across these events was "
            f"{worst_continuity_error_bps:+.2f} bps."
        )

    if review is not None:
        n_additions = int(review["n_additions"])
        n_deletions = int(review["n_deletions"])
        turnover = float(review["one_way_turnover"])
        sentences.append(
            "This date was also a periodic constituent review: "
            f"{n_additions} addition{'s' if n_additions != 1 else ''}, "
            f"{n_deletions} deletion{'s' if n_deletions != 1 else ''}, one-way "
            f"turnover {turnover:.2%}."
        )

    return " ".join(sentences)


def _row(frame: pd.DataFrame, label: Any) -> pd.Series:
    """A single row by index label, typed unambiguously.

    `frame.loc[label]` alone is typed by pandas-stubs as `Series[Any] | DataFrame`,
    because the `.loc` overloads can't tell a scalar label from a list of labels from
    the argument's static type - `label` here is usually the result of `.idxmax()`, an
    `int`, or a `pd.Timestamp` filter, all of which are always scalar in this module.
    The `isinstance` assert is not just for mypy's benefit: a frame with a duplicate
    index label would make `.loc[label]` return a `DataFrame` instead, and every
    caller here would then silently read the wrong thing out of it.
    """
    row = frame.loc[label]
    assert isinstance(row, pd.Series), f"expected a single row for label {label!r}"
    return row


def _as_date(value: Any) -> dt.date:
    """A frame's `date` cell - a `pd.Timestamp` after the parquet round trip, in
    practice - into a plain `dt.date`. Mirrors the private helper of the same name in
    `corpactions.events` and `calc.index`; kept local rather than imported because it
    is three lines and each module already carries its own copy."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.Timestamp(value).date()


def _split_detected_by(value: str) -> tuple[str, ...]:
    """The exact reverse of the join `run_chaos_drill` performs on `DrillResult.
    detected_by` before putting it in a DataFrame column (`", ".join(...)`, right after
    the `frame = pd.DataFrame(...)` line in `quality/faults.py`) - the same string
    shape `chaos_precomputed.json` stores it in. Kept local because nothing in
    `quality.faults` hands back the tuple form once a run has gone through a
    DataFrame."""
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _to_bps(ratio: float) -> float:
    """Convert a level ratio (``close / open - 1``) into a basis-point display figure.

    The one place in this module allowed to touch a bps conversion directly. No column
    `desk/snapshot.py` writes carries the whole day's (or the whole history's)
    close-over-close move already scaled to bps - only the audit trail's *event-level*
    figures are precomputed that way. Everything else in this module reads a bps figure
    straight off a column; this helper exists only for the one figure that has no
    library equivalent, using the same scaling convention `calc.state.DivisorChange`
    already uses for the same purpose (`level_continuity_error_bps`,
    `realised_return_bps`) - so a reader comparing the two sees one convention, not two.
    """
    return ratio * 10_000  # desk-arithmetic-allowlist: sanctioned, see docstring above
