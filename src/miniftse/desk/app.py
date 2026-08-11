"""FastAPI wiring for the ops desk: lifespan, static/template mounting, health, and the
error handlers that keep a visitor from ever seeing a raw traceback.

No index mathematics, no validation logic and no retrieval logic lives here - every
number on every page is produced by a library call reached through `state.py` (and,
from Task 4 on, `services.py`). This module's job is HTTP, HTML and process wiring.

**Deviation from the plan's sketch.** The plan shows a module-level `app =
create_app()` for uvicorn to import directly. A module-level call would hard-code one
`data_dir` (the default `Path("desk/data")`) into a singleton every importer shares -
`create_app`'s own docstring is why that would be a problem even though `desk/data/`
is committed to the repository (Task 14) and loading it no longer risks a
`FileNotFoundError`: tests construct an app against a `tmp_path` snapshot, or a
`tmp_path` with no snapshot at all to exercise the missing-snapshot startup path
(`test_missing_snapshot_refuses_to_start`), and neither can share one fixed,
already-bound module-level object. Only `create_app` is exported; every entry point
calls it itself, and uvicorn is run in factory mode: `uvicorn miniftse.desk.app:
create_app --factory` (see the Makefile's `desk-serve` target and the Dockerfile's
`desk` stage CMD).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio.to_thread
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from miniftse.desk.limits import (
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    MAX_SEED,
    MIN_SEED,
    TokenBucketLimiter,
    enforce_rate_limit,
    validate_date,
    validate_fault_id,
    validate_question,
    validate_question_id,
    validate_seed,
)
from miniftse.desk.services import (
    ask,
    available_dates,
    chaos_drill_rows,
    draft_questions,
    explain_day,
    notable_days,
    precomputed_drill_row,
    render_draft,
    run_drill,
)
from miniftse.desk.state import DeskState, load_desk_state
from miniftse.universe.banding import CUMULATIVE_PCT_DECIMALS

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

_DRILL_CONCURRENCY = 2
"""How many live chaos drills may run at once, across every request this process is
serving. `services.run_drill` re-runs the full validation engine against a fresh copy
of the baseline - not free - so an unbounded fan-out of concurrent POSTs could starve
the process; a small fixed pool is the plan's answer, not a queue or a rate limiter."""

_DRILL_TIMEOUT_SECONDS = 10.0
"""How long `/chaos/run` waits for a live drill before falling back to the precomputed
row for the same fault (see `chaos_run` below). A module-level name, not a literal
inline at the call site, so a test can shrink it (`monkeypatch.setattr`) to exercise the
timeout path without an actually-slow drill."""


_EXAMPLE_QUESTIONS: tuple[dict[str, object], ...] = (
    {"text": "How is free float applied to index weights?", "out_of_scope": False},
    {
        "text": "What triggers a fast entry addition between reviews?",
        "out_of_scope": False,
    },
    # Deliberately out of scope - the exact case `agents/rag.py`'s `_scope_violation`
    # documents as the reason its scope check runs before retrieval at all: this
    # shares all its vocabulary with the corpus, so a visitor sees a genuine refusal
    # rather than one they had to think up themselves.
    {"text": "What is the index level today?", "out_of_scope": True},
)
"""Seeded on `/ask` (Task 9) so a visitor sees a real answer and a real refusal without
typing either - one of the three is deliberately out of scope, per the design spec's
Screen 3 section."""

_DIVERGENCE_INCIDENT: dict[str, Any] = {
    "title": "The September 2024 constituent divergence",
    "status": "Closed",
    "source": "docs/superpowers/specs/2026-08-11-ops-desk-design.md",
    "review_date": "2024-09-20",
    "constituents_pinned": 154,
    "constituents_rebuild": 153,
    "level_continuity_bps": 0.0,
    "max_divisor_divergence_bps": 0.7426,
    "divergent_dates": 65,
    "total_dates": 2311,
    "conclusion": (
        "A rule whose outcome depends on floating-point summation order is not a rule."
    ),
    "quantisation_decimals": CUMULATIVE_PCT_DECIMALS,
    # What re-pinning the master after the fix actually moved, on one machine. Kept
    # with the incident because it is the strongest evidence the fix was worth making:
    # the limb added on pattern-matching grounds was the limb that had already fired.
    #
    # Note `computed_distance`: this is NOT a knife-edge quantity. The last-ranked
    # security's cumulative share is always exactly 1.0, and `boundary + buffer_width`
    # is exactly 1.0, so `abs(1.0 - 0.98) == 0.020000000000000018` is what IEEE-754
    # gives on every platform, every run. The pre-fix comparison failed by construction
    # whenever that configuration arose; what is rare is the configuration, not the
    # arithmetic. An earlier draft of this page called it "0.6 ulp" and framed it as a
    # coin-flip, which was wrong in a way that flattered the old code.
    "repin": {
        "reviews_replayed": 37,
        "band_changes": 1,
        "security": "SEC00012",
        "review": "December 2024",
        "cumulative_share": 1.0,
        "boundary": 0.98,
        "buffer_width": 0.02,
        "computed_distance": 0.020000000000000018,
        "was_band": "Micro Cap",
        "now_band": "Small Cap",
        "level_move_bps": 0.0115,
        "divisor_move_bps": 0.4534,
        "dates_affected": 5,
        "total_dates": 2311,
        "hash_before": "6950d93e9cd4b13b3c25b72f602d8c44",
        "hash_after": "7f0d834899e50a771e2fff1c2ec6e6d4",
    },
    "causes": (
        {
            "ref": "universe/banding.py:81",
            "code": "cumulative = np.cumsum([v for _, v in ordered]) / total",
            "problem": (
                "numpy sums in blocks whose size and association depend on the SIMD "
                "width and the BLAS build, so the last bits of the cumulative "
                "percentile differ between platforms for identical inputs."
            ),
            "now": "universe/banding.py:116",
            "fix": (
                "_exact_cumulative - Shewchuk exact partial sums, every prefix "
                "correctly rounded, so the result depends only on the values and "
                "never on how the machine associated the additions."
            ),
        },
        {
            "ref": "universe/banding.py:112",
            "code": "if cum <= cutoff:",
            "problem": (
                "a bare comparison against the band cutoff carrying no tolerance and "
                "no tie-break. A security sitting on the cut landed on opposite sides "
                "on the two platforms, and nothing in the ground rules said which side "
                "was correct."
            ),
            "now": "universe/banding.py:221",
            "fix": (
                "both sides quantised to a documented precision, and the tie-break "
                "written into Ground Rules 2.1.1: a security exactly on a cutoff "
                "belongs to the band that cutoff closes."
            ),
        },
        {
            "ref": "universe/banding.py:142",
            "code": "return abs(cum - boundary) <= width",
            "problem": (
                "the buffer-zone test carried the same exposure as the cutoff test. "
                "Included by looking for the pattern rather than because a second "
                "incident had reported it - and it turned out to have already fired "
                "silently on the reference build. See the re-pin note below."
            ),
            "now": "universe/banding.py:260",
            "fix": (
                "the same treatment, and the tie-break written into Ground Rules 8.3: "
                "a security exactly one buffer width past a boundary has not crossed "
                "it by more than the buffer, so the incumbent is held."
            ),
        },
    ),
}
"""The written record behind Screen 4, kept here rather than in the template so the
numbers have one home and the tests can assert against them.

**Two tiers of evidence, and the page must not blur them.** The `causes`/divergence
figures are the *historical, pre-fix* cross-platform comparison, reproduced from
`source` - this repository holds no independent record of that incident, and observing
it needs two platforms where only one was available. The `repin` figures are different:
they were measured on this machine and can be re-derived from the committed golden
master by anyone with the repo. `source` is rendered on the page so a reader can tell
which is which, rather than having to assume both carry the same authority.

Neither is derived from `state.golden_diff`: that payload is the live comparison for the
build being served, and conflating it with either tier is precisely the misreading the
template's "historical" labelling exists to prevent.

`quantisation_decimals` is imported from `universe.banding`, not restated, so the
precision the page publishes cannot drift from the precision the code applies.
"""

_SCHEME_LABELS: dict[str, str] = {
    "float_market_cap": "Float market cap",
    "equal": "Equal weight",
    "fundamental": "Fundamental",
    "score_tilt": "Score tilt",
    "selection": "Selection",
    "optimised": "Optimised",
}
"""Display labels for `weighting.schemes.SCHEME_PROPERTIES`'s keys, rendered on
`/index`'s Capacity section. Copied from the capacity-viz plan's Task 12
(`render_capacity.js`'s `SCHEME_LABELS`) rather than reinvented, so the two would read
identically if the static site is ever built. `SCHEME_PROPERTIES` itself is not
imported here - `desk/snapshot.py`'s `_capacity` already wrote it verbatim into
`capacity.json`, and this dict only supplies the six keys' human-readable names,
which the JSON payload does not carry."""


def _embed_json(payload: Any) -> str:
    """`payload` as JSON text safe to embed inside a `<script type="application/json">`
    tag on `/index` (the overview chart's series, and the capacity constituents/params
    the fund-size slider in `static/capacity.js` reads).

    `</` is escaped to `<\\/` so a value containing the literal substring `</script>`
    cannot prematurely close the tag it sits inside - the standard mitigation for this
    class of injection. This is needed on top of Jinja2's default autoescaping because
    autoescaping only touches HTML markup characters (`<`, `>`, `&`, quotes) in values
    interpolated directly into the template; text already inside a `{{ ... | safe }}`
    JSON blob is not re-escaped, and vanilla Jinja2 (unlike Flask) ships no `tojson`
    filter to do this for us.
    """
    return json.dumps(payload).replace("</", "<\\/")


def create_app(
    data_dir: Path = Path("desk/data"),
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    rate_limit_clock: Callable[[], float] | None = None,
) -> FastAPI:
    """Build the ops desk application.

    Nothing is loaded from `data_dir` until the returned app's `lifespan` runs - the
    call itself is cheap, so constructing an app in a test with a `tmp_path` snapshot
    (or a `tmp_path` with no snapshot at all, to exercise the missing-snapshot path)
    costs nothing until a `TestClient` actually starts it.

    `rate_limit_per_minute` and `rate_limit_clock` exist for tests, not deployment: the
    deployed process always gets `limits.DEFAULT_RATE_LIMIT_PER_MINUTE` (60) and the
    real wall clock via these parameters' defaults. A test that wants to prove the 429
    path fires needs a real budget and a frozen/advanceable clock (`rate_limit_clock`);
    a test that merely needs a *working* app - the module-scoped `client` fixture, which
    fires a couple dozen POSTs across the whole test module, nowhere near the deployed
    budget - never touches either parameter.
    """
    data_dir = Path(data_dir)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        desk = load_desk_state(data_dir)
        app.state.desk = desk
        # One limiter, shared by all three POST routes below (via the
        # `enforce_rate_limit` dependency reading `request.app.state.limiter`) - not one
        # instantiated per route, which would give each route its own 60/minute budget
        # instead of one 60/minute budget per client across all three.
        app.state.limiter = TokenBucketLimiter(
            rate_per_minute=rate_limit_per_minute,
            clock=rate_limit_clock or time.monotonic,
        )
        # Every template extends base.html, and base.html's footer (git sha, build
        # date) and nav need the snapshot without every route handler threading it
        # through by hand - a Jinja global, set once `desk` is known, does that.
        templates.env.globals["desk"] = desk
        yield

    app = FastAPI(title="miniftse ops desk", lifespan=lifespan)
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Bounds how many live chaos drills (`/chaos/run`, below) may run at once across
    # every request this app instance serves. Built once, here - not inside the route
    # handler, which would hand every request its own fresh semaphore and bound
    # nothing at all.
    app.state.drill_semaphore = asyncio.Semaphore(_DRILL_CONCURRENCY)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/day")

    @app.get("/day")
    async def day(request: Request, date: str | None = None) -> Response:
        """Screen 1: explain one session's level and divisor moves.

        `date` is validated against the snapshot's closed set of published dates
        *before* `services.explain_day` is ever called - a date this index never
        published a level for, or a string that is not a date at all, becomes a 400
        here, not a `KeyError` the service would otherwise raise (see `explain_day`'s
        docstring) and not a 500. No index arithmetic happens in this handler: it
        picks a date, calls `services.explain_day`/`services.notable_days`, and hands
        the result to the template.

        The parse-and-membership check itself is `limits.validate_date` (Task 13
        centralised it there, alongside `/chaos/run`'s and `/ask/query`'s validation) -
        this handler only supplies the closed set (`dates`) to validate against.
        """
        desk: DeskState = request.app.state.desk
        dates = available_dates(desk)
        notable = notable_days(desk)

        # No date requested: the most recently published session, so the screen opens
        # on "today" rather than an arbitrary or empty state. Otherwise, validate.
        selected = dates[-1] if date is None else validate_date(date, dates)

        explanation = explain_day(desk, selected)
        return templates.TemplateResponse(
            request,
            "day.html",
            {
                "selected_date": selected,
                "dates": dates,
                "notable": notable,
                "explanation": explanation,
            },
        )

    @app.get("/chaos")
    async def chaos(request: Request) -> Response:
        """Screen 2: the chaos-drill console.

        Renders `desk/snapshot.py`'s precomputed battery (`services.chaos_drill_rows`)
        plus its coverage gaps - no drill runs on this GET. The live re-run lives
        entirely behind the `/chaos/run` POST below; this handler does no more than
        `/day`'s does: gather what services.py already computed, hand it to the
        template.
        """
        desk: DeskState = request.app.state.desk
        return templates.TemplateResponse(
            request,
            "chaos.html",
            {
                "rows": chaos_drill_rows(desk),
                "gaps": desk.chaos_precomputed["gaps"],
                "summary": desk.chaos_precomputed["summary"],
                "seed": desk.chaos_precomputed["seed"],
                # `limits.MIN_SEED`/`MAX_SEED` are the one authority for `/chaos/run`'s
                # seed range - threaded through rather than restated as literals in the
                # template, so the rendered input's `min`/`max` cannot silently drift
                # from what `validate_seed` actually enforces server-side.
                "min_seed": MIN_SEED,
                "max_seed": MAX_SEED,
            },
        )

    @app.post("/chaos/run", dependencies=[Depends(enforce_rate_limit)])
    async def chaos_run(
        request: Request,
        fault_id: str = Form(...),
        seed: str = Form(...),
    ) -> Response:
        """Screen 2's live drill: validate the raw form input, run one fault through
        `services.run_drill` in a worker thread, and render the result fragment.

        `seed` is declared `str`, not `int`: FastAPI would otherwise coerce the form
        field itself via Pydantic and answer a non-numeric value with 422, but the plan
        requires 400 for a bad `fault_id` or an out-of-range/non-integer `seed` - so
        both raw strings are validated by `limits.validate_fault_id`/`validate_seed`
        below instead of trusting Pydantic coercion. (A form field missing entirely is a
        different failure mode: FastAPI's `Form(...)` still 422s before this handler
        body ever runs, since that happens at request-parsing time, not at the
        value-validation time this docstring is about.)

        `dependencies=[Depends(enforce_rate_limit)]` is Task 13's per-IP token bucket
        (`limits.TokenBucketLimiter`, one instance shared by all three POST routes via
        `app.state.limiter`): a client past 60 requests/minute gets a 429 before this
        body runs at all, never a 500 and never the drill machinery below.

        The live drill runs behind `app.state.drill_semaphore` (bounds concurrency to
        `_DRILL_CONCURRENCY`) and `asyncio.wait_for(..., timeout=_DRILL_TIMEOUT_SECONDS)`
        (bounds latency), via `anyio.to_thread.run_sync` so the synchronous drill -
        `services.run_drill` reruns the whole validation engine - never blocks the event
        loop. On timeout, the precomputed row for the same fault is rendered instead,
        with a visible badge - a 200, never a hang and never an error page.

        Concurrency tradeoff, made deliberately, not discovered later: the semaphore
        only genuinely bounds requests that finish inside the timeout budget.
        `anyio.to_thread.run_sync` defaults to `abandon_on_cancel=False`, so when
        `wait_for` gives up at `_DRILL_TIMEOUT_SECONDS`, the worker thread running
        `services.run_drill` is *not* killed - Python cannot forcibly kill a running
        thread - it keeps executing to completion in the background and its result is
        discarded. Because the timeout branch's `return` sits inside `async with
        semaphore`, leaving that block releases the permit immediately, before the
        orphaned thread actually finishes. A burst of near-simultaneous timeouts can
        therefore let more than `_DRILL_CONCURRENCY` drills run at once for a window
        bounded by the drill's own runtime (and, in practice, further capped by
        anyio's own worker-thread pool limit). Holding the permit until the orphaned
        thread finishes would need a detached background task with its own error
        handling, outliving this request/response cycle, to close a failure mode (many
        simultaneous timeouts) judged rare enough not to justify that machinery here.

        No drill logic lives here: validate, call `services.run_drill` (or, on timeout,
        `services.precomputed_drill_row`), render. Both live entirely in services.py.
        """
        desk: DeskState = request.app.state.desk

        validate_fault_id(fault_id)
        seed_value = validate_seed(seed)

        semaphore: asyncio.Semaphore = request.app.state.drill_semaphore
        async with semaphore:
            try:
                outcome = await asyncio.wait_for(
                    anyio.to_thread.run_sync(run_drill, desk, fault_id, seed_value),
                    timeout=_DRILL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                return templates.TemplateResponse(
                    request,
                    "partials/drill_result.html",
                    {
                        "timed_out": True,
                        "precomputed": precomputed_drill_row(desk, fault_id),
                        "seed": seed_value,
                        # The precomputed row was computed at DRILL_SEED
                        # (`snapshot.py`), not at the `seed_value` the visitor's timed-
                        # out live request asked for - the two can differ, so the
                        # fallback badge must say which seed the row it is showing
                        # actually came from.
                        "precomputed_seed": desk.chaos_precomputed["seed"],
                    },
                )

        return templates.TemplateResponse(
            request,
            "partials/drill_result.html",
            {"timed_out": False, "outcome": outcome, "seed": seed_value},
        )

    @app.get("/ask")
    async def ask_get(request: Request) -> Response:
        """Screen 3a: the methodology assistant's question form.

        Renders the form (posts to `/ask/query` via HTMX) and `_EXAMPLE_QUESTIONS` - no
        retrieval runs on this GET, the same split `/chaos` keeps between its GET and
        its live-drill POST.
        """
        return templates.TemplateResponse(
            request, "ask.html", {"examples": _EXAMPLE_QUESTIONS}
        )

    @app.post("/ask/query", dependencies=[Depends(enforce_rate_limit)])
    async def ask_query(request: Request, question: str = Form(...)) -> Response:
        """Screen 3a's live query: validate the raw form input, call `services.ask`,
        render the result fragment.

        `question` is validated by `limits.validate_question`, the same way
        `/chaos/run` validates `fault_id` and `seed`: an empty or whitespace-only
        question, or one over `limits.MAX_QUESTION_LENGTH` characters, is a 400 - never
        FastAPI's default 422 and never a call into `services.ask` with input that was
        never going to produce a real answer.

        An abstention (`RetrievedAnswer.abstained`) is not an error - the scope check or
        an empty retrieval inside `MethodologyAssistant.ask` produced a real answer to
        this question, just one that says no. `partials/answer.html` renders it as its
        own styled card, the same visual weight as a normal answer, per the design
        spec's Screen 3 section.

        No retrieval logic lives here: validate, call `services.ask`, render. All of it
        lives in `agents/rag.py`'s `MethodologyAssistant`, reached through
        `services.ask`. `dependencies=[Depends(enforce_rate_limit)]` is the same
        Task 13 rate limit `/chaos/run` carries - see that route's docstring.
        """
        stripped = validate_question(question)

        desk: DeskState = request.app.state.desk
        result = ask(desk, stripped)
        return templates.TemplateResponse(
            request, "partials/answer.html", {"result": result}
        )

    @app.get("/evals")
    async def evals(request: Request) -> Response:
        """Screen 3b: the eval report over the same assistant `/ask` answers with.

        `desk.evals` is exactly `desk/snapshot.py`'s `_evals_payload` (Task 2) -
        headline metrics, the `by_category` breakdown, every failing case with its
        `CaseResult.explain()` string already computed, and the `corpus` this build's
        assistant was scored against. No grading logic runs here, and nothing is
        recomputed: the whole payload is read straight off `state.evals`, the same way
        `/chaos`'s GET reads `desk.chaos_precomputed` directly rather than through a
        service function - there is no calculation between the precomputed JSON and the
        template for either screen.

        Showing every failure, not just the passes, is the point of this screen (see
        the design spec's Screen 3 section - "a scoreboard that only shows the wins is
        marketing"), so `evals.html` renders the failures list in an always-visible
        section, never behind a collapsed `<details>`.
        """
        desk: DeskState = request.app.state.desk
        return templates.TemplateResponse(request, "evals.html", {"evals": desk.evals})

    @app.get("/draft")
    async def draft_get(request: Request) -> Response:
        """Screen 3c: the canned-question picker, with the first question's fact pack,
        draft and `GuardResult` rendered directly - not left as an empty placeholder
        behind an HTMX round trip, so the screen has real content on first load.

        Selecting a different question, or toggling `inject_bad_number`, both live
        entirely behind the `/draft/render` POST below - the same split `/chaos` keeps
        between its GET (precomputed content only) and its live-drill POST. No drafting
        or guard logic runs here: `services.render_draft` composes it.
        """
        desk: DeskState = request.app.state.desk
        outcome = render_draft(desk, question_id=0, inject_bad_number=False)
        return templates.TemplateResponse(
            request,
            "draft.html",
            {"questions": draft_questions(), "outcome": outcome},
        )

    @app.post("/draft/render", dependencies=[Depends(enforce_rate_limit)])
    async def draft_render(
        request: Request,
        question_id: str = Form(...),
        inject_bad_number: bool = Form(False),
    ) -> Response:
        """Screen 3c's live re-render: validate the raw `question_id`, call
        `services.render_draft`, render the result fragment.

        `question_id` is declared `str`, not `int`, for the same reason `/chaos/run`
        declares `seed` a `str` (see that route's docstring): FastAPI's own Pydantic
        coercion would answer a non-numeric value with 422, but the plan requires 400
        for every bad input on this route, so `limits.validate_question_id` parses it by
        hand below. `inject_bad_number` is a plain `bool` - unlike `question_id`, a
        malformed value for it is not a validation case this plan tests, and Pydantic's
        own boolean coercion (accepting "true"/"on"/etc.) already matches an HTML
        checkbox's submitted value.

        No drafting or guard logic lives here: validate `question_id`, call
        `services.render_draft` (which raises `IndexError` for one outside
        `DRAFT_QUESTIONS`, turned into a 400 below, the same pattern `/chaos/run` uses
        for an unrecognised `fault_id`), render. The demonstration sentence
        `inject_bad_number=True` appends, and whether `NumberGuard` accepts or rejects
        it, are both entirely `render_draft`'s doing - this handler never inspects the
        outcome. `dependencies=[Depends(enforce_rate_limit)]` is the same Task 13 rate
        limit `/chaos/run` and `/ask/query` carry - see `chaos_run`'s docstring.
        """
        qid = validate_question_id(question_id)

        desk: DeskState = request.app.state.desk
        try:
            outcome = render_draft(desk, qid, inject_bad_number)
        except IndexError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return templates.TemplateResponse(
            request, "partials/draft_result.html", {"outcome": outcome}
        )

    @app.get("/reproducibility")
    async def reproducibility(request: Request) -> Response:
        """Screen 4: the pinned golden master against this build, and the incident that
        made the band rule reproducible.

        `desk.golden_diff` is exactly `desk/snapshot.py`'s `_golden_payload` - the
        master's provenance and a `ComparisonResult` already computed - read straight
        off state with nothing recomputed here, the same way `/evals` reads
        `desk.evals`. No comparison runs on this GET; `golden.compare` ran when the
        snapshot was built.

        `{"pinned": false}` is a *state*, not an error: a repository can legitimately
        have no master yet (a fresh clone before `miniftse pin-golden`, the same case
        `TestGoldenMaster.test_reference_master_if_pinned` skips on). The template
        branches on `golden.pinned` and the route does not - there is nothing for a
        handler to validate here, unlike `/day`'s closed set of dates.

        `_DIVERGENCE_INCIDENT` is passed alongside it and is deliberately *not* derived
        from the comparison: it is a written historical record of a fixed defect, so it
        renders identically whether or not a master is currently pinned, and it must
        never be mistaken for the live numbers in the panel above it.
        """
        desk: DeskState = request.app.state.desk
        return templates.TemplateResponse(
            request,
            "reproducibility.html",
            {"golden": desk.golden_diff, "incident": _DIVERGENCE_INCIDENT},
        )

    @app.get("/index")
    async def index_tab(request: Request) -> Response:
        """Screen 5: the index at a glance - overview, constituents, capacity, and risk
        & attribution, as four in-page sections reached by anchor nav rather than a
        second HTMX round trip per tab (the plan's "pick the simpler" instruction for
        this task).

        No index mathematics happens here and nothing is recomputed: all four sections
        render straight from the JSON payloads `desk/snapshot.py` already wrote
        (`state.overview`, `state.constituents`, `state.capacity`,
        `state.risk_attribution`) - the same "read precomputed state, nothing
        recalculated" pattern `/evals`, `/chaos`'s GET and `/reproducibility` already
        follow. `_SCHEME_LABELS` supplies only display names for the six scheme keys
        `capacity.json` already carries; every trade-off description rendered in that
        table is `capacity["schemes"]` verbatim.

        The one exception - the only place in the ops desk where index-adjacent
        arithmetic runs outside the library - is the Capacity section's fund-size
        slider: `static/capacity.js` recomputes the capacity-trim algorithm entirely in
        the browser as the slider moves, because there is no way to serve that live
        without a network round trip per pixel of drag. `overview_chart_json` and
        `capacity_slider_json` below are the two payloads that JS on this page reads
        directly (the level-history series for Chart.js, and the constituent
        weights/adv/capacity_params for the slider) - both pre-serialised here, not in
        the template, so the escaping in `_embed_json` has exactly one call site to
        reason about.
        """
        desk: DeskState = request.app.state.desk
        return templates.TemplateResponse(
            request,
            "index_tab.html",
            {
                "overview": desk.overview,
                "constituents": desk.constituents,
                "capacity": desk.capacity,
                "risk_attribution": desk.risk_attribution,
                "scheme_labels": _SCHEME_LABELS,
                "overview_chart_json": _embed_json({
                    "dates": desk.overview["dates"],
                    "pr": desk.overview["pr"],
                    "gtr": desk.overview["gtr"],
                    "ntr": desk.overview["ntr"],
                }),
                "capacity_slider_json": _embed_json({
                    "constituents": desk.capacity["constituents"],
                    "capacity_params": desk.capacity["capacity_params"],
                }),
            },
        )

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        """Liveness plus proof of *which* snapshot is being served - the git sha the
        deployed container was built from, not just that the process answers."""
        desk: DeskState = request.app.state.desk
        return {
            "status": "ok",
            "snapshot_git_sha": desk.manifest["git_sha"],
            "loaded_at": desk.loaded_at.isoformat(),
        }

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        """Covers 404 (no route matched) and every explicit `HTTPException` a route
        raises (the closed-set validation in later tasks: bad `date`, bad `fault_id`,
        an out-of-range `seed`) - one template, rendered inside the site layout rather
        than Starlette's bare default page."""
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"status_code": exc.status_code, "detail": str(exc.detail)},
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        """Anything not already an `HTTPException`. A visitor gets the same styled
        error card as a 404, not a traceback. Starlette's `ServerErrorMiddleware` sends
        this response and then re-raises the original exception on its way out, so it
        still reaches the process's logs (and, in a test with default settings, the
        test itself) - only what the visitor sees changes."""
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"status_code": 500, "detail": "Something went wrong on our end."},
            status_code=500,
        )

    return app
