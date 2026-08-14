"""Announcement text -> structured corporate action.

Three properties, in descending order of how much they matter:

1. **Malformed output abstains; it never raises and never half-parses.** A partially
   built event is the failure that grades cleanly and is wrong.
2. **Abstention is a first-class outcome.** Some announcements genuinely do not state
   the terms an event needs. Declining is correct there and is measured separately.
3. **Every field is validated through `taxonomy.build_event`.** The model proposes a
   type and a payload; the taxonomy decides whether that is constructible. The model
   never instantiates an event directly.

Rule 1 of `agents/llm.py` still holds: no number here reaches a client-facing output.
These numbers go to the engine, which recomputes the index itself.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from miniftse.agents.llm import LlmClient, Message
from miniftse.corpactions.events import CorporateAction, EventType
from miniftse.triage.taxonomy import TAXONOMY, TaxonomyError, build_event

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
        parsed: dict[str, Any] = json.loads(raw.strip())
    except (json.JSONDecodeError, AttributeError):
        return Extraction(None, True, "could not parse model output as JSON", raw)

    if parsed.get("abstain"):
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
    except (KeyError, TypeError, ValueError, TaxonomyError) as exc:
        return Extraction(None, True, str(exc), raw)

    return Extraction(event, False, "", raw)
