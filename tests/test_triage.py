"""Corporate action triage: taxonomy, grading, corpus."""

from __future__ import annotations

import datetime as dt
import json as _json
from pathlib import Path

import pandas as pd
import pytest

from miniftse.calc.state import Constituent, IndexState
from miniftse.corpactions.events import (
    CashDividend,
    Delisting,
    EventType,
    ReturnOfCapital,
    Spinoff,
    Split,
)
from miniftse.triage.corpus import (
    Announcement,
    LabelSource,
    Provenance,
    read_jsonl,
    write_jsonl,
)
from miniftse.triage.labels import LabelledEvent, harvest_labels
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

    def test_round_trips_a_cash_dividend_with_non_default_optional_fields(
        self, tmp_path: Path
    ) -> None:
        """`currency` and `withholding_rate` are optional dataclass fields, not in
        `TAXONOMY[CASH_DIVIDEND].required`. A round trip must not silently revert
        them to their defaults ("USD", 0.0)."""
        label = CashDividend(**COMMON, amount=2.0, currency="EUR", withholding_rate=0.15)
        announcement = Announcement(
            announcement_id="A4",
            security_id="S0",
            text="A EUR cash dividend of 2.00 per share, withheld at 15%.",
            provenance=PROV,
            label=label,
            label_source=LabelSource.AUTO,
        )
        path = tmp_path / "corpus.jsonl"

        write_jsonl(path, [announcement])
        loaded = read_jsonl(path)

        assert loaded[0].label == label
        assert isinstance(loaded[0].label, CashDividend)
        assert loaded[0].label.currency == "EUR"
        assert loaded[0].label.withholding_rate == 0.15

    def test_round_trips_a_spinoff_that_does_not_enter_the_index(self, tmp_path: Path) -> None:
        """`spinco_enters_index=False` is not in `TAXONOMY[SPINOFF].required` and
        directly flips `is_divisor_event`. Losing it on read would change what the
        grading engine computes, not just what the label displays."""
        label = Spinoff(
            **COMMON,
            spinco_security_id="S1",
            shares_per_parent_share=0.5,
            value_per_parent_share=10.0,
            parent_cum_price=100.0,
            spinco_enters_index=False,
        )
        announcement = Announcement(
            announcement_id="A5",
            security_id="S0",
            text="Spin-off of S1; the new shares will not join the index.",
            provenance=PROV,
            label=label,
            label_source=LabelSource.MANUAL,
        )
        path = tmp_path / "corpus.jsonl"

        write_jsonl(path, [announcement])
        loaded = read_jsonl(path)

        assert loaded[0].label == label
        assert isinstance(loaded[0].label, Spinoff)
        assert loaded[0].label.spinco_enters_index is False
        assert loaded[0].label.is_divisor_event is True

    def test_round_trips_a_delisting_with_a_nonzero_final_price(self, tmp_path: Path) -> None:
        """`Delisting`'s `required` is empty, so before this fix `final_price` and
        `reason` were the two fields dropped on every single delisting label,
        regardless of what was recorded. A real final price must survive."""
        label = Delisting(**COMMON, final_price=3.5, reason="SCHEME_OF_ARRANGEMENT")
        announcement = Announcement(
            announcement_id="A6",
            security_id="S0",
            text="Delisted under a scheme of arrangement at 3.50 per share.",
            provenance=PROV,
            label=label,
            label_source=LabelSource.MANUAL,
        )
        path = tmp_path / "corpus.jsonl"

        write_jsonl(path, [announcement])
        loaded = read_jsonl(path)

        assert loaded[0].label == label
        assert isinstance(loaded[0].label, Delisting)
        assert loaded[0].label.final_price == 3.5
        assert loaded[0].label.reason == "SCHEME_OF_ARRANGEMENT"

    def test_round_trips_a_special_dividend_preserving_is_special(self, tmp_path: Path) -> None:
        """`is_special` is a type-discriminating field restored via `spec.defaults`,
        not carried as ordinary payload - confirm that still holds now that payload
        carries every other optional field too."""
        label = CashDividend(**COMMON, amount=9.0, is_special=True)
        announcement = Announcement(
            announcement_id="A7",
            security_id="S0",
            text="A special dividend of $9.00 per share.",
            provenance=PROV,
            label=label,
            label_source=LabelSource.AUTO,
        )
        path = tmp_path / "corpus.jsonl"

        write_jsonl(path, [announcement])
        loaded = read_jsonl(path)

        assert loaded[0].label == label
        assert isinstance(loaded[0].label, CashDividend)
        assert loaded[0].label.is_special is True
        assert loaded[0].label.event_type is EventType.SPECIAL_DIVIDEND


class FakeActionsProvider:
    """Returns the exact frame shape YFinanceProvider.get_corp_actions produces."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def get_corp_actions(
        self, security_ids: list[str] | None, start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def _row(event_type: str, payload: dict[str, object], ticker: str = "AAPL") -> dict[str, object]:
    return {
        "event_id": f"YF-{event_type}-{ticker}",
        "security_id": ticker,
        "event_type": event_type,
        "announcement_date": D,
        "ex_date": D,
        "pay_date": D,
        "payload": _json.dumps(payload),
    }


class TestFreeLabels:
    def test_harvests_a_dividend_and_a_split(self) -> None:
        provider = FakeActionsProvider(
            [
                _row("CASH_DIVIDEND", {"amount": 0.24}),
                _row("SPLIT", {"ratio": 4.0}),
            ]
        )

        labels = harvest_labels(provider, ["AAPL"], D, D)

        assert len(labels) == 2
        assert {label.event.event_type for label in labels} == {
            EventType.CASH_DIVIDEND,
            EventType.SPLIT,
        }
        assert all(isinstance(label, LabelledEvent) for label in labels)

    def test_skips_a_row_it_cannot_build_rather_than_guessing(self) -> None:
        provider = FakeActionsProvider(
            [
                _row("CASH_DIVIDEND", {}),  # no amount
                _row("CASH_DIVIDEND", {"amount": 0.24}),
            ]
        )

        labels = harvest_labels(provider, ["AAPL"], D, D)

        assert len(labels) == 1
        assert labels[0].event.amount == 0.24

    def test_empty_frame_yields_nothing(self) -> None:
        assert harvest_labels(FakeActionsProvider([]), ["AAPL"], D, D) == []
