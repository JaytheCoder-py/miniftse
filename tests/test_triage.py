"""Corporate action triage: taxonomy, grading, corpus."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from miniftse.calc.state import Constituent, IndexState
from miniftse.corpactions.events import CashDividend, EventType, ReturnOfCapital, Split
from miniftse.triage.corpus import (
    Announcement,
    LabelSource,
    Provenance,
    read_jsonl,
    write_jsonl,
)
from miniftse.triage.taxonomy import TAXONOMY, build_event
from miniftse.triage.verify import impact_error

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


def make_state(n: int = 3, price: float = 100.0, shares: float = 1000.0) -> IndexState:
    constituents = {f"S{i}": Constituent(f"S{i}", price=price, shares=shares) for i in range(n)}
    return IndexState.initialise(D, constituents, base_level=1000.0)


class TestImpactError:
    def test_return_of_capital_booked_as_a_dividend(self) -> None:
        """The canonical misclassification, hand-computed.

        Three constituents at 100.00 x 1,000 shares -> market value 300,000.
        Base level 1,000 -> divisor 300.

        A 2.00 distribution on S0 takes its price to 98.00, so market value becomes
        98,000 + 100,000 + 100,000 = 298,000 either way. The classification decides
        what the divisor does:

        * CashDividend      is_divisor_event False -> divisor stays 300
                            level = 298,000 / 300         = 993.333333
        * ReturnOfCapital   is_divisor_event True  -> divisor rebases to preserve the
                            level implied by 300,000: 298,000 / (300,000/300) = 298
                            level = 298,000 / 298         = 1,000.000000

        error = |1,000.000000 - 993.333333| / 993.333333 x 10,000 = 67.114 bps

        Same amount, same price effect, same date. 67bp of published index level,
        purely from the label.
        """
        state = make_state()
        truth = CashDividend(**COMMON, amount=2.0)
        predicted = ReturnOfCapital(**COMMON, amount=2.0)

        result = impact_error(predicted, truth, state)

        assert result.truth_level == pytest.approx(993.333333, abs=1e-5)
        assert result.predicted_level == pytest.approx(1000.0, abs=1e-5)
        assert result.error_bps == pytest.approx(67.114, abs=0.01)
        assert result.same_type is False

    def test_identical_events_score_zero(self) -> None:
        state = make_state()
        truth = CashDividend(**COMMON, amount=2.0)
        predicted = CashDividend(**COMMON, amount=2.0)

        result = impact_error(predicted, truth, state)

        assert result.error_bps == pytest.approx(0.0, abs=1e-9)
        assert result.same_type is True

    def test_a_wrong_amount_on_the_right_type_is_small(self) -> None:
        """2.00 vs 2.10 on one of three names. Right type, wrong number: the error is
        real but two orders of magnitude below a misclassification."""
        state = make_state()
        truth = CashDividend(**COMMON, amount=2.0)
        predicted = CashDividend(**COMMON, amount=2.1)

        result = impact_error(predicted, truth, state)

        assert result.same_type is True
        assert 0.0 < result.error_bps < 5.0


PROV = Provenance(
    source="yfinance",
    url="https://example.invalid/actions/S0",
    retrieved=dt.date(2026, 8, 13),
    sha256="0" * 64,
)


class TestCorpus:
    def test_round_trips_a_labelled_announcement(self, tmp_path: Path) -> None:
        announcement = Announcement(
            announcement_id="A1",
            security_id="S0",
            text="The Board declared a quarterly cash dividend of $2.00 per share.",
            provenance=PROV,
            label=CashDividend(**COMMON, amount=2.0),
            label_source=LabelSource.AUTO,
        )
        path = tmp_path / "corpus.jsonl"

        write_jsonl(path, [announcement])
        loaded = read_jsonl(path)

        assert len(loaded) == 1
        assert loaded[0].announcement_id == "A1"
        assert loaded[0].text == announcement.text
        assert loaded[0].provenance == PROV
        assert loaded[0].label_source is LabelSource.AUTO
        assert isinstance(loaded[0].label, CashDividend)
        assert loaded[0].label.amount == 2.0
        assert loaded[0].label.ex_date == D

    def test_round_trips_an_unlabelled_announcement(self, tmp_path: Path) -> None:
        announcement = Announcement(
            announcement_id="A2",
            security_id="S1",
            text="Some announcement nobody has labelled yet.",
            provenance=PROV,
        )
        path = tmp_path / "corpus.jsonl"

        write_jsonl(path, [announcement])
        loaded = read_jsonl(path)

        assert loaded[0].label is None
        assert loaded[0].label_source is LabelSource.UNLABELLED

    def test_text_is_never_empty(self) -> None:
        with pytest.raises(ValueError, match="text"):
            Announcement(announcement_id="A3", security_id="S0", text="  ", provenance=PROV)
