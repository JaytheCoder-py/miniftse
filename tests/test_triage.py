"""Corporate action triage: taxonomy, grading, corpus."""

from __future__ import annotations

import datetime as dt

import pytest

from miniftse.corpactions.events import CashDividend, EventType, Split
from miniftse.triage.taxonomy import TAXONOMY, build_event

D = dt.date(2024, 6, 10)

COMMON = {
    "event_id": "E1",
    "security_id": "S0",
    "ex_date": D,
    "announcement_date": dt.date(2024, 5, 20),
    "pay_date": dt.date(2024, 6, 24),
}


class TestTaxonomy:
    def test_covers_every_event_type(self) -> None:
        assert set(TAXONOMY) == set(EventType)

    def test_unmapped_types_are_explicit(self) -> None:
        for event_type, spec in TAXONOMY.items():
            if spec.handler is None:
                assert spec.note, f"{event_type} has no handler and no note explaining it"
                assert spec.in_scope_stage is None

    def test_builds_a_cash_dividend(self) -> None:
        event = build_event(EventType.CASH_DIVIDEND, COMMON, {"amount": 2.0})
        assert isinstance(event, CashDividend)
        assert event.amount == 2.0
        assert event.event_type is EventType.CASH_DIVIDEND

    def test_builds_a_special_dividend_from_the_same_class(self) -> None:
        event = build_event(EventType.SPECIAL_DIVIDEND, COMMON, {"amount": 9.0})
        assert isinstance(event, CashDividend)
        assert event.event_type is EventType.SPECIAL_DIVIDEND

    def test_builds_a_split(self) -> None:
        event = build_event(EventType.SPLIT, COMMON, {"ratio": 10.0})
        assert isinstance(event, Split)
        assert event.ratio == 10.0

    def test_rejects_a_missing_required_field(self) -> None:
        with pytest.raises(ValueError, match="amount"):
            build_event(EventType.CASH_DIVIDEND, COMMON, {})

    def test_rejects_an_unmapped_type(self) -> None:
        unmapped = [t for t, s in TAXONOMY.items() if s.handler is None]
        if not unmapped:
            pytest.skip("every event type has a handler")
        with pytest.raises(ValueError, match="no handler"):
            build_event(unmapped[0], COMMON, {})
