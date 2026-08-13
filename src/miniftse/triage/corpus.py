"""The labelled announcement corpus.

JSONL rather than a database: the corpus is small, it wants to be diffable in review,
and a labelling disagreement should show up in `git diff` as one changed line.

Provenance is mandatory on every row. An announcement whose source cannot be checked
cannot be used to argue that the model got it right.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from miniftse.corpactions.events import CorporateAction, EventType
from miniftse.triage.taxonomy import COMMON_FIELDS, build_event

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


class LabelSource(StrEnum):
    AUTO = "auto"
    """Derived from structured vendor data - splits and dividends. Cheap and plentiful."""
    MANUAL = "manual"
    """Hand-labelled from the announcement text. Expensive; reserved for the classes
    where the label is actually in doubt."""
    UNLABELLED = "unlabelled"


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    url: str
    retrieved: dt.date
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "url": self.url,
            "retrieved": self.retrieved.isoformat(),
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Provenance:
        return cls(
            source=raw["source"],
            url=raw["url"],
            retrieved=dt.date.fromisoformat(raw["retrieved"]),
            sha256=raw["sha256"],
        )


@dataclass(frozen=True, slots=True)
class Announcement:
    announcement_id: str
    security_id: str
    text: str
    provenance: Provenance
    label: CorporateAction | None = None
    label_source: LabelSource = LabelSource.UNLABELLED

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("announcement text is empty")

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "announcement_id": self.announcement_id,
            "security_id": self.security_id,
            "text": self.text,
            "provenance": self.provenance.to_dict(),
            "label_source": str(self.label_source),
        }
        if self.label is None:
            row["label"] = None
            return row

        common = {f: getattr(self.label, f) for f in COMMON_FIELDS}
        # Every declared field beyond COMMON_FIELDS, not just `spec.required` - an
        # optional field left at a non-default value (Spinoff.spinco_enters_index,
        # Delisting.final_price, CashDividend.withholding_rate) must survive the
        # round trip too, or the stored label silently becomes a different event.
        # `CorporateAction` is an ABC, not itself a dataclass, so it does not statically
        # satisfy `fields()`'s `DataclassInstance` protocol even though every concrete
        # subclass is `@dataclass`-decorated and satisfies it at runtime.
        payload = {
            f.name: getattr(self.label, f.name)
            for f in fields(cast("DataclassInstance", self.label))
            if f.name not in COMMON_FIELDS
        }
        row["label"] = {
            "event_type": str(self.label.event_type),
            "common": {
                k: v.isoformat() if isinstance(v, dt.date) else v for k, v in common.items()
            },
            "payload": {
                k: v.isoformat() if isinstance(v, dt.date) else v for k, v in payload.items()
            },
        }
        return row

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Announcement:
        label: CorporateAction | None = None
        raw_label = raw.get("label")
        if raw_label is not None:
            common = {
                k: dt.date.fromisoformat(v) if k.endswith("_date") else v
                for k, v in raw_label["common"].items()
            }
            label = build_event(EventType(raw_label["event_type"]), common, raw_label["payload"])
        return cls(
            announcement_id=raw["announcement_id"],
            security_id=raw["security_id"],
            text=raw["text"],
            provenance=Provenance.from_dict(raw["provenance"]),
            label=label,
            label_source=LabelSource(raw["label_source"]),
        )


def write_jsonl(path: Path, announcements: list[Announcement]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for announcement in announcements:
            fh.write(json.dumps(announcement.to_dict(), sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[Announcement]:
    with path.open(encoding="utf-8") as fh:
        return [Announcement.from_dict(json.loads(line)) for line in fh if line.strip()]
