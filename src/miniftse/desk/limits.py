"""Rate limiting and centralised input validation for the ops desk.

Two things live here, both because Task 13's plan asked for them together and both are
small enough that a second module would be ceremony:

1. `TokenBucketLimiter` - an in-process, per-IP token bucket applied to the three POST
   routes (`/chaos/run`, `/ask/query`, `/draft/render`) via the `enforce_rate_limit`
   FastAPI dependency below. No Redis: this app is single-process (see `app.py`'s module
   docstring on why `create_app` is a factory, not a module-level singleton), and a
   distributed limiter would be infrastructure for a problem this process doesn't have.

2. The closed-set validation predicates and numeric limits Tasks 5, 7, 9 and 10 each
   wrote inline in their own route handler: `date` against the snapshot's published
   dates, `fault_id` against the known chaos faults, `seed`'s range, `question`'s length
   cap, `question_id`'s shape. Moving them here gives every input rule one home to read
   and one place to change a limit - `app.py`'s routes now call these functions instead
   of repeating the literals (`500`, `999_999`, ...) at each call site.

Moving the validation here does not change what a bad request gets back: every function
below raises the same `HTTPException` with the same status code and detail text the
route used to raise inline, so the existing tests that assert on `/day`, `/chaos/run`,
`/ask/query` and `/draft/render`'s 400s are the proof this refactor changed nothing a
visitor can observe.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Sequence

from fastapi import HTTPException, Request

from miniftse.quality.faults import FAULTS

# ---------------------------------------------------------------------------------------
# Centralised validation: closed sets and numeric limits, one name per rule.
# ---------------------------------------------------------------------------------------

VALID_FAULT_IDS: frozenset[str] = frozenset(fault.fault_id for fault in FAULTS)
"""The closed set `/chaos/run` validates a requested `fault_id` against - a bad id must
never reach `services.run_drill` (and the semaphore/thread/timeout machinery behind it)
at all. Formerly `app.py`'s module-level `_VALID_FAULT_IDS`."""

MIN_SEED = 0
MAX_SEED = 999_999
"""`/chaos/run`'s inclusive range for `seed`. Formerly the bare literals `0` and
`999_999` inline in that route."""

MAX_QUESTION_LENGTH = 500
"""`/ask/query`'s server-side cap on `question`'s length, enforced by hand rather than a
Pydantic `Field(max_length=...)` - see `validate_question`'s docstring for why. Formerly
`app.py`'s module-level `_MAX_QUESTION_LENGTH`."""


def validate_date(date_str: str, available: Sequence[dt.date]) -> dt.date:
    """`/day`'s validation: `date_str` must parse as an ISO date *and* be one of
    `available` (the snapshot's published dates) - the same two-stage shape every
    closed-set check in this module follows: reject unparseable input, then reject
    input that parses but isn't in the known set. Returns the parsed date so the route
    never re-parses what this function already validated.
    """
    try:
        parsed = dt.date.fromisoformat(date_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"'{date_str}' is not a valid date (expected YYYY-MM-DD).",
        ) from exc
    if parsed not in available:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{parsed.isoformat()} is not a date this index published a level for."
            ),
        )
    return parsed


def validate_fault_id(fault_id: str) -> None:
    """`/chaos/run`'s validation: `fault_id` must be one of `VALID_FAULT_IDS`."""
    if fault_id not in VALID_FAULT_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"'{fault_id}' is not a known chaos-drill fault id.",
        )


def validate_seed(seed: str) -> int:
    """`/chaos/run`'s validation: `seed` must be an integer string in
    `[MIN_SEED, MAX_SEED]`. Declared as a hand-validated `str` rather than
    `int = Form(...)` at the route so a non-numeric value is this function's 400, not
    FastAPI's default 422 - see `app.py`'s `chaos_run` docstring for the full reasoning.
    Returns the parsed int so the route never re-parses it.
    """
    try:
        seed_value = int(seed)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"'{seed}' is not a valid seed (expected an integer).",
        ) from exc
    if not (MIN_SEED <= seed_value <= MAX_SEED):
        raise HTTPException(
            status_code=400,
            detail=f"seed must be between {MIN_SEED} and {MAX_SEED}, got {seed_value}.",
        )
    return seed_value


def validate_question(question: str) -> str:
    """`/ask/query`'s validation: `question` must not be empty or whitespace-only, and
    must be at most `MAX_QUESTION_LENGTH` characters. Returns the stripped question so
    the route hands `services.ask` the same trimmed text this function checked.
    """
    stripped = question.strip()
    if not stripped:
        raise HTTPException(status_code=400, detail="question must not be empty.")
    if len(question) > MAX_QUESTION_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=(
                f"question must be at most {MAX_QUESTION_LENGTH} characters, "
                f"got {len(question)}."
            ),
        )
    return stripped


def validate_question_id(question_id: str) -> int:
    """`/draft/render`'s validation: `question_id` must be an integer string. Declared
    as a hand-validated `str`, not `int = Form(...)`, for the same 400-vs-422 reason
    `validate_seed` is. Range checking against `DRAFT_QUESTIONS` is *not* done here -
    `services.render_draft` raises `IndexError` for an out-of-range id, which the route
    turns into a 400 itself (see `app.py`'s `draft_render`), the same pattern
    `/chaos/run` uses for an unrecognised `fault_id` it can't check without the drill
    faults list `services` already owns.
    """
    try:
        return int(question_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{question_id}' is not a valid question id (expected an integer)."
            ),
        ) from exc


# ---------------------------------------------------------------------------------------
# Rate limiting: in-process per-IP token bucket.
# ---------------------------------------------------------------------------------------

DEFAULT_RATE_LIMIT_PER_MINUTE = 60
"""The deployed rate limit for the three POST routes. `create_app`'s default - tests
that need a different budget (generous, so the shared `client` fixture's handful of
POSTs can never collide with a real limit; or tiny, to exercise the 429 path without
firing dozens of requests) pass their own value through `create_app`'s
`rate_limit_per_minute` parameter instead of changing this constant."""

_DEFAULT_IDLE_PRUNE_SECONDS = 300.0
"""How long a per-IP bucket may sit untouched before `TokenBucketLimiter` drops it. A
long-lived deployment sees a constant trickle of distinct client IPs; without pruning,
`_buckets` grows for as long as the process runs. Five minutes is comfortably longer
than the 60-second window a bucket needs to refill to full capacity, so pruning a bucket
this idle throws away nothing a fresh one wouldn't already give that IP."""


class TokenBucketLimiter:
    """An in-process, per-IP token bucket: `rate_per_minute` tokens capacity, refilling
    continuously at `rate_per_minute` tokens per 60 seconds, one bucket per key.

    `clock` is injectable (a zero-argument callable returning seconds, matching
    `time.monotonic`'s signature) so tests can freeze or fast-forward time instead of
    sleeping for real - the 61st call in the same instant must deterministically 429,
    and "the bucket refills after 60 seconds" must be provable without a 60-second test.
    Production code never passes `clock`; it defaults to `time.monotonic`, which is
    immune to wall-clock adjustments (NTP, DST) that `time.time` is not.
    """

    def __init__(
        self,
        rate_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        idle_after_seconds: float = _DEFAULT_IDLE_PRUNE_SECONDS,
    ) -> None:
        if rate_per_minute <= 0:
            raise ValueError(f"rate_per_minute must be positive, got {rate_per_minute}")
        self._capacity = float(rate_per_minute)
        self._refill_per_second = rate_per_minute / 60.0
        self._clock = clock
        self._idle_after_seconds = idle_after_seconds
        # key -> (tokens remaining, last time this key was touched)
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        """`True` and consumes one token if `key` has one to spend; `False` (and
        consumes nothing) if its bucket is empty. Also sweeps buckets idle longer than
        `idle_after_seconds` before deciding - see the class docstring on why an evicted
        bucket is behaviourally identical to one that simply refilled.
        """
        now = self._clock()
        self._prune(now)
        tokens, last_seen = self._buckets.get(key, (self._capacity, now))
        elapsed = max(0.0, now - last_seen)
        tokens = min(self._capacity, tokens + elapsed * self._refill_per_second)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._buckets[key] = (tokens, now)
        return allowed

    def _prune(self, now: float) -> None:
        stale = [
            key
            for key, (_, last_seen) in self._buckets.items()
            if now - last_seen > self._idle_after_seconds
        ]
        for key in stale:
            del self._buckets[key]


async def enforce_rate_limit(request: Request) -> None:
    """FastAPI dependency wired onto `/chaos/run`, `/ask/query` and `/draft/render`
    only - never a `GET`. Reads the one `TokenBucketLimiter` `create_app`'s lifespan put
    on `app.state.limiter` (all three routes share it, not one bucket set each) and
    raises **429** the moment a key's bucket is empty.

    Declared `async def`, not a plain `def`. FastAPI runs a plain-`def` dependency in
    the threadpool, and `TokenBucketLimiter._buckets` is an unlocked dict read-modified-
    written by `allow()` - concurrent requests would race on it for real. An `async def`
    dependency instead runs straight on the event loop; since this function contains no
    `await` (every line here is dict lookups and float arithmetic - see `allow()`'s own
    docstring on why nothing here blocks), it cannot be interleaved with another call to
    itself, so the race is gone with no lock needed.

    Client identity is `request.client.host` - the peer address the ASGI server reports
    for the connection. Whether that address is the actual visitor depends on what sits
    in front of the server: with nothing in front (a direct connection - `make
    desk-serve`'s local run), it already is the visitor's address and needs no further
    configuration. Behind a reverse proxy, every request instead arrives from the
    proxy's own address unless the ASGI server is explicitly told to trust that proxy's
    `X-Forwarded-For` header and substitute it for `request.client` - which is exactly
    what uvicorn's `--proxy-headers`/`--forwarded-allow-ips` flags do, and exactly what
    the Dockerfile's `desk` stage CMD passes for the deployed service.
    Without that configured trust, per-IP limiting behind a proxy would bucket every
    visitor together under the proxy's one address; blindly trusting a client-supplied
    header with no proxy in front would let any visitor claim to be any IP for free -
    this dependency does neither, it relies entirely on the ASGI server's own trust
    configuration to make `request.client.host` mean the right thing in each setting.

    **That trust is currently mis-configured on the deployed service, and this limiter is
    best-effort until it is fixed** (DECISIONS.md D-017, reproduction in
    `desk/README.md`). The CMD passes `--forwarded-allow-ips=*`, and uvicorn resolves a
    fully-trusted `X-Forwarded-For` to its *leftmost* entry - which is whatever the caller
    sent, because proxies append to that header rather than replacing it. A caller
    rotating the header therefore gets a fresh bucket per request; measured on uvicorn
    0.52.1, 65 requests under a rotating forged header drew zero 429s. Nothing about this
    module is wrong - `request.client.host` is the right key, and it is the ASGI server's
    job to make that mean the visitor - but a reader should not take the presence of this
    dependency as proof that per-IP limiting is actually in force in production.
    """
    limiter: TokenBucketLimiter = request.app.state.limiter
    host = request.client.host if request.client is not None else "unknown"
    if not limiter.allow(host):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment and try again.",
        )
