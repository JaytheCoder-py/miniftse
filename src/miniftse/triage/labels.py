"""Free labels: splits and dividends from structured vendor data.

The economics of the eval set live here. Hand-labelling every announcement would cost
more hours than the project has, and most of those hours would go on cash dividends,
where the label is never in doubt. Vendor data labels those for nothing, which leaves
the manual budget for spin-offs, rights issues and mergers - the classes where the
label is actually the hard part.

What this does NOT give you is announcement text. `get_corp_actions` returns the event,
not the press release that announced it. `text.py` performs that join, and anything it
cannot join is dropped rather than paired with a guess.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Protocol

import pandas as pd

from miniftse.corpactions.events import CorporateAction, EventType
from miniftse.triage.taxonomy import TaxonomyError, build_event


class ActionsProvider(Protocol):
    """The slice of MarketDataProvider this module needs."""

    def get_corp_actions(
        self, security_ids: list[str] | None, start: dt.date, end: dt.date
    ) -> pd.DataFrame: ...


@dataclass(frozen=True, slots=True)
class LabelledEvent:
    """A ground-truth event with no text attached yet."""

    event: CorporateAction
    security_id: str
    source: str
    raw_event_id: str


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.Timestamp(value).date()


def harvest_labels(
    provider: ActionsProvider,
    security_ids: list[str],
    start: dt.date,
    end: dt.date,
    *,
    source: str = "vendor-actions",
) -> list[LabelledEvent]:
    """Build ground-truth events from a provider's corporate actions frame.

    A row that cannot be built is skipped, not defaulted. A dividend with no amount is
    missing data; inventing one produces a label that grades cleanly and is wrong,
    which corrupts every metric downstream.
    """
    frame = provider.get_corp_actions(security_ids, start, end)
    if frame.empty:
        return []

    labels: list[LabelledEvent] = []
    for row in frame.to_dict("records"):
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        common = {
            "event_id": str(row["event_id"]),
            "security_id": str(row["security_id"]),
            "ex_date": _as_date(row["ex_date"]),
            "announcement_date": _as_date(row["announcement_date"]),
            "pay_date": _as_date(row["pay_date"]),
        }
        try:
            event = build_event(EventType(row["event_type"]), common, payload)
        except (TaxonomyError, ValueError):
            continue
        labels.append(
            LabelledEvent(
                event=event,
                security_id=str(row["security_id"]),
                source=source,
                raw_event_id=str(row["event_id"]),
            )
        )
    return labels
