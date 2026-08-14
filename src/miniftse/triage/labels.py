"""Free labels: splits and dividends from structured vendor data.

The economics of the eval set live here. Hand-labelling every announcement would cost
more hours than the project has, and most of those hours would go on cash dividends,
where the label is never in doubt. Vendor data labels those for nothing, which leaves
the manual budget for spin-offs, rights issues and mergers - the classes where the
label is actually the hard part.

What this does NOT give you is announcement text. `get_corp_actions` returns the event,
not the press release that announced it. `text.py` performs that join, and anything it
cannot join is dropped rather than paired with a guess.

Rows that cannot be built are dropped **and counted**, the same contract
`join_labels_to_text` keeps for rows that cannot be joined. A corpus that quietly
shrank by 98% because one vendor field was unrecognised would still produce a
scoreboard, and the scoreboard would look fine. See D-031.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Protocol

import pandas as pd

from miniftse.corpactions.events import CorporateAction, EventType
from miniftse.triage.taxonomy import build_event


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


@dataclass(frozen=True, slots=True)
class SkippedRow:
    """A vendor row that produced no label, and why.

    Kept rather than counted only, because the useful question when a harvest comes
    back small is never "how many" on its own - it is "which event types", which is
    what tells you whether the corpus lost its long tail or lost its bulk.
    """

    event_id: str
    security_id: str
    event_type: str
    reason: str
    """Short category, for grouping: `unknown_event_type` or `unbuildable`."""

    detail: str
    """The exception message. High-cardinality; use `reason` to group."""


def skip_counts(skipped: list[SkippedRow]) -> dict[str, int]:
    """Skipped rows per `reason:event_type`, for the harvest's one-line summary."""
    counter: collections.Counter[str] = collections.Counter(
        f"{row.reason}:{row.event_type}" for row in skipped
    )
    return dict(sorted(counter.items()))


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
) -> tuple[list[LabelledEvent], list[SkippedRow]]:
    """Build ground-truth events from a provider's corporate actions frame.

    Returns `(labels, skipped)`. A row that cannot be built is skipped, not defaulted:
    a dividend with no amount is missing data, and inventing one produces a label that
    grades cleanly and is wrong, which corrupts every metric downstream. But the skips
    are returned rather than swallowed - `join_labels_to_text` already hands back its
    `unjoined`, `extract_event` already returns `abstained` and a `reason`, and this was
    the one fail-closed stage in the pipeline whose losses were invisible. The
    difference matters exactly when it is largest: a single unrecognised vendor field
    once made 98% of the repo's own corpus unbuildable, and a bare `list` return said
    nothing at all about it.
    """
    frame = provider.get_corp_actions(security_ids, start, end)
    if frame.empty:
        return [], []

    labels: list[LabelledEvent] = []
    skipped: list[SkippedRow] = []
    for row in frame.to_dict("records"):
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        event_id, security_id = str(row["event_id"]), str(row["security_id"])
        raw_type = str(row["event_type"])
        common = {
            "event_id": event_id,
            "security_id": security_id,
            "ex_date": _as_date(row["ex_date"]),
            "announcement_date": _as_date(row["announcement_date"]),
            "pay_date": _as_date(row["pay_date"]),
        }
        try:
            event_type = EventType(raw_type)
        except ValueError as exc:
            skipped.append(
                SkippedRow(event_id, security_id, raw_type, "unknown_event_type", str(exc))
            )
            continue
        try:
            event = build_event(event_type, common, payload)
        except (TypeError, ValueError) as exc:
            # `TaxonomyError` is a `ValueError` subclass, so naming both would be
            # redundant. `TypeError` is not: a handler dataclass raises it, not
            # `ValueError`, when the assembled kwargs do not fit its signature - and
            # letting one such row propagate is what aborted the whole harvest before.
            skipped.append(
                SkippedRow(
                    event_id, security_id, raw_type, "unbuildable", f"{type(exc).__name__}: {exc}"
                )
            )
            continue
        labels.append(
            LabelledEvent(
                event=event,
                security_id=security_id,
                source=source,
                raw_event_id=event_id,
            )
        )
    return labels, skipped
