"""Announcement text -> structured corporate action.

Three properties, in descending order of how much they matter:

1. **Malformed output abstains; it never raises and never half-parses.** A partially
   built event is the failure that grades cleanly and is wrong.
2. **Abstention is a first-class outcome.** Some announcements genuinely do not state
   the terms an event needs. Declining is correct there and is measured separately.
3. **Every field is validated through `taxonomy.build_event`.** The model proposes a
   type and a payload; the taxonomy decides whether that is constructible. The model
   never instantiates an event directly.
4. **Identity is the caller's, never the model's.** `security_id` and `event_id` go
   into `common` from this function's own arguments, and `build_event` drops any
   `COMMON_FIELDS` key a payload carries, so the returned event is always an answer to
   the question that was asked. A model that echoes a different `security_id` back
   would otherwise be graded against a different constituent's index weight. See D-033.

Rule 1 of `agents/llm.py` still holds: no number here reaches a client-facing output.
These numbers go to the engine, which recomputes the index itself.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from miniftse.agents.llm import LlmClient, Message
from miniftse.corpactions.events import CorporateAction, EventType
from miniftse.triage.taxonomy import TAXONOMY, build_event

SYSTEM = """You classify corporate action announcements for an equity index.

Return ONLY a JSON object, no prose. Either:

  {"event_type": "<TYPE>", "ex_date": "YYYY-MM-DD", "announcement_date": "YYYY-MM-DD",
   "pay_date": "YYYY-MM-DD", "payload": {...}}

or, when the announcement does not state what is needed:

  {"abstain": true, "reason": "<short reason>"}

Abstain rather than guess. A confident wrong classification moves a published index
level; a declined one costs an analyst two minutes.
"""


@dataclass(frozen=True, slots=True)
class Extraction:
    event: CorporateAction | None
    abstained: bool
    reason: str = ""
    raw: str = ""


def _prompt(text: str, security_id: str) -> str:
    types = ", ".join(str(t) for t, s in TAXONOMY.items() if s.handler is not None)
    return f"Security: {security_id}\nPermitted event_type values: {types}\n\nAnnouncement:\n{text}"


def extract_event(
    client: LlmClient,
    announcement_text: str,
    security_id: str,
    *,
    event_id: str,
    max_tokens: int = 512,
) -> Extraction:
    raw = client.complete(
        [Message("user", _prompt(announcement_text, security_id))],
        system=SYSTEM,
        max_tokens=max_tokens,
        temperature=0.0,
    ).text

    try:
        parsed = json.loads(raw.strip())
    except (json.JSONDecodeError, AttributeError):
        return Extraction(None, True, "could not parse model output as JSON", raw)

    # `json.loads` happily parses `[1, 2, 3]`, `"hello"`, `42` and `null` - all valid
    # JSON, none of them the object this function's contract requires. A `dict[str,
    # Any]` annotation on `parsed` does not check that at runtime, so without this
    # guard `parsed.get(...)` below raises `AttributeError` on any of those four
    # shapes: valid JSON that abstention and parsing alike must reject, not crash on.
    if not isinstance(parsed, dict):
        return Extraction(
            None,
            True,
            f"model output was valid JSON but not an object (got {type(parsed).__name__})",
            raw,
        )

    if parsed.get("abstain") is True:
        return Extraction(None, True, str(parsed.get("reason", "abstained")), raw)

    try:
        common = {
            "event_id": event_id,
            "security_id": security_id,
            "ex_date": dt.date.fromisoformat(parsed["ex_date"]),
            "announcement_date": dt.date.fromisoformat(parsed["announcement_date"]),
            "pay_date": dt.date.fromisoformat(parsed["pay_date"]),
        }
        event = build_event(EventType(parsed["event_type"]), common, parsed.get("payload", {}))
    except (KeyError, TypeError, ValueError) as exc:
        # `TaxonomyError` subclasses `ValueError`, so naming it as well would be
        # redundant - the same tidy D-031 made to `labels.py:132`. `KeyError` and
        # `TypeError` are not redundant and are both load-bearing: `parsed["ex_date"]`
        # raises `KeyError` on a model that omits a date entirely, and
        # `dt.date.fromisoformat(20240610)` raises `TypeError` for a JSON number where
        # a date string was asked for - both before `build_event` is reached at all.
        return Extraction(None, True, str(exc), raw)

    return Extraction(event, False, "", raw)
