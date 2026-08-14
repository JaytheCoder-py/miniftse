"""Corporate action triage: taxonomy, grading, corpus."""

from __future__ import annotations

import datetime as dt
import hashlib
import json as _json
from pathlib import Path

import pandas as pd
import pytest

from miniftse.agents.llm import LlmClient, LlmResponse, Message
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
from miniftse.triage.extract import extract_event
from miniftse.triage.labels import LabelledEvent, harvest_labels
from miniftse.triage.taxonomy import TAXONOMY, build_event
from miniftse.triage.text import FilingDocument, _strip_html, join_labels_to_text
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

    def test_rejects_a_wrong_typed_required_field(self) -> None:
        """Key *presence* alone is not enough: `spec.required` only checks that
        `amount` is in the payload, and a dataclass field accepts a value of any
        type at construction. Without a type check, `{"amount": "two dollars"}`
        would build a `CashDividend` with a string `amount` and never raise -
        exactly the "partially built event that grades cleanly and is wrong" this
        module's own docstring names as the worst outcome."""
        with pytest.raises(ValueError, match="amount"):
            build_event(EventType.CASH_DIVIDEND, COMMON, {"amount": "two dollars"})

    def test_accepts_an_int_where_a_float_is_required(self) -> None:
        """Guards against over-tightening: `json.loads('{"amount": 2}')` produces a
        Python `int` for a whole-number literal, and a model has no reason to write
        "2.0" for a $2 dividend. `CashDividend.amount` is annotated `float`; an
        `int` payload value must still build successfully."""
        event = build_event(EventType.CASH_DIVIDEND, COMMON, {"amount": 2})
        assert isinstance(event, CashDividend)
        assert event.amount == 2

    def test_rejects_a_bool_for_a_numeric_required_field(self) -> None:
        """`bool` is an `int` subclass in Python, so a naive `isinstance(value,
        (int, float))` check would silently accept `True`/`False` as a numeric
        `amount` - `CashDividend(amount=True)` constructs cleanly as a $1.00
        dividend. `bool` must be rejected for a numeric field even though the
        `int`-for-`float` relaxation above exists."""
        with pytest.raises(ValueError, match="amount"):
            build_event(EventType.CASH_DIVIDEND, COMMON, {"amount": True})


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


def _doc(filed: dt.date, text: str = "Board declares dividend.") -> FilingDocument:
    return FilingDocument(
        accession=f"0000-{filed.isoformat()}",
        filed=filed,
        url=f"https://www.sec.gov/Archives/{filed.isoformat()}",
        text=text,
        security_id="AAPL",
    )


class TestTextJoin:
    def test_joins_a_filing_inside_the_window(self) -> None:
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        document = _doc(dt.date(2024, 5, 19))  # announcement_date is 2024-05-20

        joined, unjoined = join_labels_to_text([label], [document], window_days=5)

        assert len(joined) == 1
        assert unjoined == []
        assert joined[0].label_source is LabelSource.AUTO
        assert joined[0].text == "Board declares dividend."
        assert joined[0].provenance.source == "sec-edgar"

    def test_drops_a_label_with_no_filing_in_the_window(self) -> None:
        """The load-bearing behaviour: a label 139 days from the only filing must be
        dropped and counted, never paired with the nearest-anyway document. A join
        that silently widened to "nearest regardless of window" would pass every
        other test in this class while corrupting the corpus invisibly - this is the
        test that would catch it."""
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        document = _doc(dt.date(2024, 1, 2))  # far outside the window

        joined, unjoined = join_labels_to_text([label], [document], window_days=5)

        assert joined == []
        assert len(unjoined) == 1
        assert unjoined[0] is label

    def test_picks_the_nearest_filing_when_several_qualify(self) -> None:
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        near = _doc(dt.date(2024, 5, 20), text="NEAR")
        far = _doc(dt.date(2024, 5, 17), text="FAR")

        joined, _ = join_labels_to_text([label], [far, near], window_days=5)

        assert joined[0].text == "NEAR"

    def test_does_not_join_across_securities(self) -> None:
        """Same date, same window, wrong issuer. If security were not filtered on,
        this MSFT filing would be the nearest-by-date candidate for an AAPL label and
        would join - exactly the wrong-issuer mistake the module's docstring warns
        about."""
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        other = FilingDocument(
            accession="X",
            filed=dt.date(2024, 5, 20),
            url="https://www.sec.gov/Archives/X",
            text="MSFT news",
            security_id="MSFT",
        )

        joined, unjoined = join_labels_to_text([label], [other], window_days=5)

        assert joined == []
        assert len(unjoined) == 1

    def test_a_filing_exactly_on_the_window_boundary_still_joins(self) -> None:
        """`abs(delta) <= window_days` - the boundary itself is inclusive, not a
        fencepost the brief left ambiguous."""
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        document = _doc(dt.date(2024, 5, 25))  # exactly 5 days after 2024-05-20

        joined, unjoined = join_labels_to_text([label], [document], window_days=5)

        assert len(joined) == 1
        assert unjoined == []

    def test_one_day_past_the_window_boundary_is_dropped(self) -> None:
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        document = _doc(dt.date(2024, 5, 26))  # 6 days after 2024-05-20

        joined, unjoined = join_labels_to_text([label], [document], window_days=5)

        assert joined == []
        assert len(unjoined) == 1

    def test_provenance_sha256_matches_the_joined_text(self) -> None:
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        document = _doc(dt.date(2024, 5, 20), text="Board declares dividend.")

        joined, _ = join_labels_to_text(
            [label], [document], window_days=5, retrieved=dt.date(2026, 8, 14)
        )

        expected = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        assert joined[0].provenance.sha256 == expected
        assert joined[0].provenance.retrieved == dt.date(2026, 8, 14)
        assert joined[0].provenance.url == document.url

    def test_two_labels_can_join_to_the_same_filing(self) -> None:
        """Two vendor events for the same security within one filing's window (e.g. a
        dividend declaration bundled with other 8-K items) both join to it. The
        `announcement_id` disambiguates them because it embeds `raw_event_id`."""
        first = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        second = LabelledEvent(
            event=Split(**{**COMMON, "security_id": "AAPL"}, ratio=2.0),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-2",
        )
        document = _doc(dt.date(2024, 5, 20))

        joined, unjoined = join_labels_to_text([first, second], [document], window_days=5)

        assert unjoined == []
        assert len(joined) == 2
        assert {a.announcement_id for a in joined} == {
            "YF-1::0000-2024-05-20",
            "YF-2::0000-2024-05-20",
        }

    def test_a_blank_only_candidate_is_treated_as_no_filing_at_all(self) -> None:
        """A document that strips down to whitespace-only text (e.g. an all-
        script/style redirect page) is not a plausible match either.
        `Announcement.__post_init__` would reject it, so it must never become `best`
        in the first place - the label lands in `unjoined`, exactly as if no filing
        existed in the window."""
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        blank = _doc(dt.date(2024, 5, 20), text="   ")  # whitespace-only

        joined, unjoined = join_labels_to_text([label], [blank], window_days=5)

        assert joined == []
        assert len(unjoined) == 1
        assert unjoined[0] is label

    def test_a_nearer_blank_candidate_loses_to_a_farther_real_one(self) -> None:
        """The blank document is the exact-date match and would win the nearest-filing
        tie-break outright - it must be excluded from candidacy entirely, not merely
        lose on distance, or a same-day empty exhibit would still beat a real filing a
        few days off."""
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        blank = _doc(dt.date(2024, 5, 20), text="")  # exact-date match, but empty
        real = _doc(dt.date(2024, 5, 17), text="Board declares dividend.")

        joined, unjoined = join_labels_to_text([label], [blank, real], window_days=5)

        assert unjoined == []
        assert len(joined) == 1
        assert joined[0].text == "Board declares dividend."

    def test_one_blank_candidate_does_not_abort_the_rest_of_the_batch(self) -> None:
        """The regression this pins: before the guard, a blank `best` candidate for
        any one label raised inside `Announcement.__post_init__`, uncaught by
        `join_labels_to_text`. That exception would propagate out of the whole call,
        discarding `joined`/`unjoined` results already computed for every other label
        in the batch - a far worse failure than the "wrong join" this module exists to
        prevent. One blocked label (its only candidate is blank) sits alongside one
        that joins cleanly and one with no candidate at all; the call must return
        complete, correct results for all three and never raise."""
        blocked = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        fine = LabelledEvent(
            event=Split(**{**COMMON, "security_id": "MSFT"}, ratio=2.0),
            security_id="MSFT",
            source="vendor-actions",
            raw_event_id="YF-2",
        )
        missing = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "GOOG"}, amount=1.0),
            security_id="GOOG",
            source="vendor-actions",
            raw_event_id="YF-3",
        )
        blank_doc = _doc(dt.date(2024, 5, 20), text="")
        good_doc = FilingDocument(
            accession="MSFT-1",
            filed=dt.date(2024, 5, 20),
            url="https://www.sec.gov/Archives/MSFT-1",
            text="Board approves a two-for-one split.",
            security_id="MSFT",
        )

        joined, unjoined = join_labels_to_text(
            [blocked, fine, missing], [blank_doc, good_doc], window_days=5
        )

        assert len(joined) == 1
        assert joined[0].security_id == "MSFT"
        assert joined[0].text == "Board approves a two-for-one split."
        assert len(unjoined) == 2
        assert {label.raw_event_id for label in unjoined} == {"YF-1", "YF-3"}


class TestStripHtml:
    def test_removes_tags(self) -> None:
        assert _strip_html("<p>Hello <b>World</b></p>") == "Hello World"

    def test_drops_script_and_style_contents(self) -> None:
        raw = "<style>.a{color:red}</style><script>var x=1;</script><p>Keep me</p>"

        result = _strip_html(raw)

        assert result == "Keep me"
        assert "color" not in result
        assert "var x" not in result

    def test_unescapes_entities(self) -> None:
        assert _strip_html("<p>Fish &amp; Chips&nbsp;Shop</p>") == "Fish & Chips Shop"

    def test_collapses_whitespace(self) -> None:
        assert _strip_html("<p>Too    much\n\n\twhitespace</p>") == "Too much whitespace"

    def test_empty_input_yields_empty_text(self) -> None:
        assert _strip_html("") == ""

    def test_a_tag_with_no_text_content_yields_empty_text(self) -> None:
        assert _strip_html("<div><span></span></div>") == ""

    def test_on_an_8k_like_fragment(self) -> None:
        """A small hand-written fragment shaped like a real 8-K body: a `<style>`
        block, a `<script>` block, nested inline tags, and both an `&amp;` and an
        `&nbsp;` entity, all in one pass."""
        raw = (
            "<html><body>\n"
            "<style>.a { color: red; }</style>\n"
            "<script>var x = 1;</script>\n"
            "<p>Item 5.02 &amp; Departure of Directors.</p>\n"
            "<p>The Board of Directors of <b>Example&nbsp;Corp</b> approved a dividend.</p>\n"
            "</body></html>"
        )

        result = _strip_html(raw)

        assert result == (
            "Item 5.02 & Departure of Directors. "
            "The Board of Directors of Example Corp approved a dividend."
        )


class ScriptedLlm(LlmClient):
    """Returns a fixed body. The extractor's contract is parse-and-validate, and that
    is what these tests exercise - not the model."""

    name = "scripted"

    def __init__(self, body: str) -> None:
        self.body = body

    def complete(
        self,
        messages: list[Message],
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse:
        return LlmResponse(text=self.body, model="scripted")


class TestExtraction:
    def test_parses_a_well_formed_dividend(self) -> None:
        client = ScriptedLlm(
            _json.dumps(
                {
                    "event_type": "CASH_DIVIDEND",
                    "ex_date": "2024-06-10",
                    "announcement_date": "2024-05-20",
                    "pay_date": "2024-06-24",
                    "payload": {"amount": 2.0},
                }
            )
        )

        result = extract_event(client, "…dividend of $2.00…", "S0", event_id="E1")

        assert result.abstained is False
        assert isinstance(result.event, CashDividend)
        assert result.event.amount == 2.0

    def test_abstains_when_the_model_says_so(self) -> None:
        client = ScriptedLlm(_json.dumps({"abstain": True, "reason": "terms not stated"}))

        result = extract_event(client, "…something ambiguous…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None
        assert "terms" in result.reason

    def test_malformed_output_abstains_rather_than_raising(self) -> None:
        result = extract_event(ScriptedLlm("not json at all"), "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None
        assert "parse" in result.reason.lower()

    def test_a_missing_required_field_abstains(self) -> None:
        client = ScriptedLlm(
            _json.dumps(
                {
                    "event_type": "CASH_DIVIDEND",
                    "ex_date": "2024-06-10",
                    "announcement_date": "2024-05-20",
                    "pay_date": "2024-06-24",
                    "payload": {},
                }
            )
        )

        result = extract_event(client, "…", "S0", event_id="E1")

        assert result.abstained is True
        assert "amount" in result.reason

    def test_an_unmapped_event_type_abstains_rather_than_raising(self) -> None:
        """`STOCK_DIVIDEND`, `TENDER_OFFER` and `SUSPENSION` are declared in
        `EventType` but `TAXONOMY[...].handler is None`. `build_event` raises
        `TaxonomyError` (a `ValueError` subclass) for these rather than constructing
        a partial event; `extract_event` must turn that raise into an abstention
        rather than let it propagate."""
        unmapped = [t for t, s in TAXONOMY.items() if s.handler is None]
        assert unmapped, "expected at least one unmapped EventType to exercise this"
        client = ScriptedLlm(
            _json.dumps(
                {
                    "event_type": str(unmapped[0]),
                    "ex_date": "2024-06-10",
                    "announcement_date": "2024-05-20",
                    "pay_date": "2024-06-24",
                    "payload": {},
                }
            )
        )

        result = extract_event(client, "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None

    def test_a_malformed_date_string_abstains_rather_than_raising(self) -> None:
        """`dt.date.fromisoformat` raises `ValueError` on a string that is not a
        valid ISO date. `extract_event` must catch that and abstain rather than let
        it propagate."""
        client = ScriptedLlm(
            _json.dumps(
                {
                    "event_type": "CASH_DIVIDEND",
                    "ex_date": "not-a-date",
                    "announcement_date": "2024-05-20",
                    "pay_date": "2024-06-24",
                    "payload": {"amount": 2.0},
                }
            )
        )

        result = extract_event(client, "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None

    def test_a_json_array_abstains_rather_than_raising(self) -> None:
        """`json.loads` parses `[1, 2, 3]` without error - it is valid JSON, just not
        an object. `parsed.get(...)` on a `list` raises `AttributeError`, uncaught
        anywhere before this fix; this must abstain instead."""
        result = extract_event(ScriptedLlm("[1, 2, 3]"), "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None

    def test_a_bare_json_string_abstains_rather_than_raising(self) -> None:
        """`"hello"` is valid JSON (a bare string), not an object."""
        result = extract_event(ScriptedLlm('"hello"'), "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None

    def test_a_bare_json_number_abstains_rather_than_raising(self) -> None:
        """`42` is valid JSON (a bare number), not an object."""
        result = extract_event(ScriptedLlm("42"), "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None

    def test_json_null_abstains_rather_than_raising(self) -> None:
        """`null` is valid JSON and parses to `None`, which has no `.get` at all -
        the shape most likely to be missed by a fix that only checks for `list`."""
        result = extract_event(ScriptedLlm("null"), "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None

    def test_a_wrong_typed_payload_field_abstains_rather_than_building(self) -> None:
        """`{"amount": "two dollars"}` satisfies `spec.required` (the key is
        present) but not the dataclass's own `float` annotation. Before the
        `taxonomy.build_event` type check, this built a `CashDividend` with a
        string `amount` and `abstained=False` - a poisoned event handed downstream
        with nothing raised anywhere; see D-028."""
        client = ScriptedLlm(
            _json.dumps(
                {
                    "event_type": "CASH_DIVIDEND",
                    "ex_date": "2024-06-10",
                    "announcement_date": "2024-05-20",
                    "pay_date": "2024-06-24",
                    "payload": {"amount": "two dollars"},
                }
            )
        )

        result = extract_event(client, "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None
        assert "amount" in result.reason

    def test_a_string_abstain_value_does_not_abstain(self) -> None:
        """`if parsed.get("abstain"):` treats any truthy value as an abstention, so
        the JSON string `"false"` (truthy in Python, however misleading the text)
        would abstain. The check must be boolean-strict: only the JSON literal
        `true` (Python `True`) abstains."""
        client = ScriptedLlm(
            _json.dumps(
                {
                    "abstain": "false",
                    "event_type": "CASH_DIVIDEND",
                    "ex_date": "2024-06-10",
                    "announcement_date": "2024-05-20",
                    "pay_date": "2024-06-24",
                    "payload": {"amount": 2.0},
                }
            )
        )

        result = extract_event(client, "…dividend of $2.00…", "S0", event_id="E1")

        assert result.abstained is False
        assert isinstance(result.event, CashDividend)

    def test_an_unexpected_payload_field_abstains_rather_than_raising(self) -> None:
        """`build_event` forwards every payload key to the handler dataclass
        (`kwargs.update(payload)`), and the system prompt names the permitted
        `event_type` values but never the payload schema for each one - nothing
        stops a model from adding a key the dataclass does not declare (e.g. a
        stray `confidence` field). The dataclass constructor then raises
        `TypeError`, not `ValueError`/`TaxonomyError`; see D-027."""
        client = ScriptedLlm(
            _json.dumps(
                {
                    "event_type": "CASH_DIVIDEND",
                    "ex_date": "2024-06-10",
                    "announcement_date": "2024-05-20",
                    "pay_date": "2024-06-24",
                    "payload": {"amount": 2.0, "confidence": 0.9},
                }
            )
        )

        result = extract_event(client, "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None
