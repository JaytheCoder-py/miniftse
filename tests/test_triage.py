"""Corporate action triage: taxonomy, grading, corpus."""

from __future__ import annotations

import datetime as dt
import hashlib
import json as _json
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest

from miniftse.agents.llm import LlmClient, LlmResponse, Message
from miniftse.calc.state import Constituent, IndexState
from miniftse.corpactions.events import (
    CashDividend,
    CorporateAction,
    Delisting,
    EventType,
    ReturnOfCapital,
    RightsIssue,
    SharesChange,
    Spinoff,
    Split,
)
from miniftse.data.synthetic import SyntheticConfig, SyntheticUniverse
from miniftse.triage.corpus import (
    Announcement,
    LabelSource,
    Provenance,
    read_jsonl,
    write_jsonl,
)
from miniftse.triage.extract import extract_event
from miniftse.triage.labels import LabelledEvent, harvest_labels, skip_counts
from miniftse.triage.taxonomy import COMMON_FIELDS, TAXONOMY, build_event
from miniftse.triage.text import FilingDocument, _strip_html, join_labels_to_text
from miniftse.triage.verify import ImpactError, Ungraded, impact_error

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

    def test_ignores_a_payload_key_the_handler_does_not_declare(self) -> None:
        """The repo's own `SyntheticUniverse` emits `gross_amount` alongside `amount`
        on every dividend (`synthetic.py:530,542`) and `terp` on every rights issue
        (`synthetic.py:596`). Neither is a constructor argument, so forwarding the
        payload verbatim made `spec.handler(**kwargs)` raise `TypeError` - which
        `harvest_labels` did not catch, so ONE such row aborted the whole harvest.
        Undeclared keys must be dropped: they describe nothing this taxonomy models,
        and rejecting the row throws away the label as well. See D-031."""
        event = build_event(
            EventType.CASH_DIVIDEND,
            COMMON,
            {"amount": 0.5, "currency": "USD", "gross_amount": 0.5, "is_special": False},
        )

        assert isinstance(event, CashDividend)
        assert event.amount == 0.5
        assert event.currency == "USD"
        assert not hasattr(event, "gross_amount")

    def test_a_declared_optional_field_still_survives_the_filter(self) -> None:
        """The filter drops what the dataclass does not declare, and nothing else. A
        blunter fix - forwarding only `spec.required` - would pass the test above and
        silently discard `withholding_rate`, reintroducing exactly the corpus-mutation
        defect D-024 exists to prevent."""
        event = build_event(
            EventType.CASH_DIVIDEND,
            COMMON,
            {"amount": 2.0, "currency": "EUR", "withholding_rate": 0.15, "terp": 94.0},
        )

        assert isinstance(event, CashDividend)
        assert event.currency == "EUR"
        assert event.withholding_rate == 0.15

    def test_a_payload_cannot_override_a_common_field(self) -> None:
        """`common` is the caller's statement of *which* event on *which* security is
        being built; `payload` is a description of the event's terms proposed by
        something else. All five `COMMON_FIELDS` are declared fields on every handler
        dataclass, so filtering the payload to declared names alone let
        `kwargs.update(payload)` overwrite every one of them - the payload won.

        `corpus.to_dict` has always excluded these five from the payload it writes
        (`corpus.py:93-97`), so the rule was known on the way out and unenforced on the
        way in. Here all five are supplied wrong at once: none may land. See D-033."""
        event = build_event(
            EventType.CASH_DIVIDEND,
            COMMON,
            {
                "amount": 2.0,
                "event_id": "HALLUCINATED",
                "security_id": "S1",
                "ex_date": dt.date(1999, 1, 1),
                "announcement_date": dt.date(1999, 1, 1),
                "pay_date": dt.date(1999, 1, 1),
            },
        )

        assert isinstance(event, CashDividend)
        assert event.amount == 2.0
        assert event.event_id == "E1"
        assert event.security_id == "S0"
        assert event.ex_date == D
        assert event.announcement_date == dt.date(2024, 5, 20)
        assert event.pay_date == dt.date(2024, 6, 24)

    def test_rejects_a_non_object_payload(self) -> None:
        """`payload: dict[str, Any]` checks nothing at runtime, and the caller nearest
        a live model passes `parsed.get("payload", {})` straight through. A JSON list
        must raise the `TaxonomyError` every caller already handles, not the
        `AttributeError` that filtering a non-mapping would otherwise produce."""
        with pytest.raises(ValueError, match="payload must be an object"):
            build_event(EventType.CASH_DIVIDEND, COMMON, [1, 2, 3])  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("event_type", "payload", "field_name"),
        [
            (EventType.SPLIT, {"ratio": 0.0}, "ratio"),
            (EventType.SPLIT, {"ratio": -2.0}, "ratio"),
            (EventType.REVERSE_SPLIT, {"ratio": 0.0}, "ratio"),
            (
                EventType.RIGHTS_ISSUE,
                {
                    "subscription_price": 70.0,
                    "new_shares": 1,
                    "per_held": 0,
                    "cum_price": 100.0,
                },
                "per_held",
            ),
            (
                EventType.SPINOFF,
                {
                    "spinco_security_id": "S1",
                    "shares_per_parent_share": 0.0,
                    "value_per_parent_share": 10.0,
                    "parent_cum_price": 100.0,
                },
                "shares_per_parent_share",
            ),
        ],
    )
    def test_rejects_a_non_positive_ratio_or_entitlement(
        self, event_type: EventType, payload: dict[str, object], field_name: str
    ) -> None:
        """Type-correct is not constructible. `0.0` is a valid `float` and built a
        `Split` that raised `ZeroDivisionError` at `events.py:228` the moment the
        engine divided by it; `per_held=0` and `shares_per_parent_share=0.0` raise
        `ValueError` at `events.py:320` and `events.py:428` the same way. A NEGATIVE
        ratio is worse still: it constructs, applies, and passes the engine's own
        market-value-invariance check, because price and share count both flip sign
        and their product is unchanged - leaving a negative price in the index. See
        D-032."""
        with pytest.raises(ValueError, match=field_name):
            build_event(event_type, COMMON, payload)

    def test_every_positive_field_is_also_a_required_field(self) -> None:
        """`_check_positive` indexes `payload[name]` directly, which is only safe
        because the missing-field check above it has already run. That holds for the
        current table; this pins it, so adding a `positive` entry for an optional
        field fails here rather than with a `KeyError` on the first payload that
        omits it."""
        for event_type, spec in TAXONOMY.items():
            assert set(spec.positive) <= set(spec.required), event_type


def make_state(n: int = 3, price: float = 100.0, shares: float = 1000.0) -> IndexState:
    constituents = {f"S{i}": Constituent(f"S{i}", price=price, shares=shares) for i in range(n)}
    return IndexState.initialise(D, constituents, base_level=1000.0)


def graded(result: ImpactError | Ungraded) -> ImpactError:
    """Assert a pair was actually graded, and narrow the union for the assertions."""
    assert isinstance(result, ImpactError), result
    return result


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

        level error    = |1,000.000000 - 993.333333| / 993.333333 x 10,000
                       = 67.114 bps
        divisor error  = |298 / 300 - 1| x 10,000 = (2/300) x 10,000
                       = 66.667 bps
        headline       = max(67.114, 66.667) = 67.114 bps, unchanged by D-029.

        Same amount, same price effect, same date. 67bp of published index level,
        purely from the label - **on this three-name fixture.** The figure is a
        property of S0's 33.3% weight as much as of the error: see
        `test_the_misclassification_penalty_is_weight_dependent_but_its_ratio_is_not`,
        which computes the same misclassification at 204.082 bps on a one-name index
        and 20.040 bps on a ten-name one. What survives the weight is the ratio
        against a misread amount, not the absolute number.
        """
        state = make_state()
        truth = CashDividend(**COMMON, amount=2.0)
        predicted = ReturnOfCapital(**COMMON, amount=2.0)

        result = graded(impact_error(predicted, truth, state))

        assert result.truth_level == pytest.approx(993.333333, abs=1e-5)
        assert result.predicted_level == pytest.approx(1000.0, abs=1e-5)
        assert result.error_bps == pytest.approx(67.114, abs=0.01)
        assert result.level_error_bps == pytest.approx(67.114, abs=0.01)
        assert result.divisor_error_bps == pytest.approx(66.667, abs=0.01)
        assert result.same_type is False
        assert result.identical_events is False

    def test_identical_events_score_zero(self) -> None:
        state = make_state()
        truth = CashDividend(**COMMON, amount=2.0)
        predicted = CashDividend(**COMMON, amount=2.0)

        result = graded(impact_error(predicted, truth, state))

        assert result.error_bps == pytest.approx(0.0, abs=1e-9)
        assert result.same_type is True
        assert result.identical_events is True

    def test_a_wrong_amount_on_the_right_type_is_small(self) -> None:
        """2.00 vs 2.10 on one of three names. Right type, wrong number: the error is
        real but an order of magnitude below a misclassification at this weight, and
        exactly 1/20th of it at every weight."""
        state = make_state()
        truth = CashDividend(**COMMON, amount=2.0)
        predicted = CashDividend(**COMMON, amount=2.1)

        result = graded(impact_error(predicted, truth, state))

        assert result.same_type is True
        assert 0.0 < result.error_bps < 5.0
        assert result.divisor_error_bps == pytest.approx(0.0, abs=1e-12)

    def test_a_split_booked_as_a_share_count_change(self) -> None:
        """A misclassification the level cannot see, hand-computed.

        Both events double S0's share count; only one of them is structural, and the
        divisor is where that shows up. From market value 300,000, divisor 300:

        * Split(ratio=2.0)                is_divisor_event False. Price 100 -> 50,
                                          shares 1,000 -> 2,000: S0's market value is
                                          100,000 either way, total stays 300,000,
                                          divisor stays 300, level 1,000.000000
        * SharesChange(1,000 -> 2,000)    is_divisor_event True. Price stays 100,
                                          shares 2,000 -> S0 is worth 200,000, total
                                          400,000, divisor rebases to
                                          300 x 400,000/300,000 = 400,
                                          level = 400,000/400 = 1,000.000000

        level error   = |1,000 - 1,000| / 1,000 x 10,000        = 0.000 bps
        divisor error = |400/300 - 1| x 10,000 = (100/300) x 10,000
                      = 3,333.333 bps

        The rebase makes the level continuous *by construction*, so a level-only
        grader scores a genuine misclassification of a stage-1 class at a perfect
        0.0000 bps. It is the divisor that diverges by a third.
        """
        state = make_state()
        truth = Split(**COMMON, ratio=2.0)
        predicted = SharesChange(**COMMON, new_shares=2000.0, old_shares=1000.0)

        result = graded(impact_error(predicted, truth, state))

        assert result.truth_level == pytest.approx(1000.0, abs=1e-9)
        assert result.predicted_level == pytest.approx(1000.0, abs=1e-9)
        assert result.level_error_bps == pytest.approx(0.0, abs=1e-9)
        assert result.truth_divisor == pytest.approx(300.0, abs=1e-9)
        assert result.predicted_divisor == pytest.approx(400.0, abs=1e-9)
        assert result.divisor_error_bps == pytest.approx(3333.3333, abs=1e-3)
        assert result.error_bps == pytest.approx(3333.3333, abs=1e-3)
        assert result.same_type is False

    def test_two_divisor_events_with_different_amounts(self) -> None:
        """Two events of the SAME type and the same divisor treatment, differing only
        in the parameter. Hand-computed from market value 300,000, divisor 300:

        * ReturnOfCapital(2.00)   S0 price 100 -> 98, total 298,000,
                                  divisor = 300 x 298,000/300,000 = 298,
                                  level = 298,000/298 = 1,000.000000
        * ReturnOfCapital(20.00)  S0 price 100 -> 80, total 280,000,
                                  divisor = 300 x 280,000/300,000 = 280,
                                  level = 280,000/280 = 1,000.000000

        level error   = 0.000 bps - both rebases hold the level at exactly 1,000.
        divisor error = |280/298 - 1| x 10,000 = (18/298) x 10,000
                      = 604.027 bps

        A tenfold error in the amount of a capital return, invisible to a level diff.
        """
        state = make_state()
        truth = ReturnOfCapital(**COMMON, amount=2.0)
        predicted = ReturnOfCapital(**COMMON, amount=20.0)

        result = graded(impact_error(predicted, truth, state))

        assert result.level_error_bps == pytest.approx(0.0, abs=1e-9)
        assert result.truth_divisor == pytest.approx(298.0, abs=1e-9)
        assert result.predicted_divisor == pytest.approx(280.0, abs=1e-9)
        assert result.divisor_error_bps == pytest.approx(604.0268, abs=1e-3)
        assert result.error_bps == pytest.approx(604.0268, abs=1e-3)
        assert result.same_type is True

    def test_a_rights_issue_with_the_wrong_terms(self) -> None:
        """The same shape on a stage-3 class, hand-computed from 300,000 / divisor 300.

        * 1-for-4 at 70, cum 100   TERP = (4x100 + 1x70)/5 = 94.
                                   Shares 1,000 x (1 + 1/4) = 1,250, so S0 is worth
                                   94 x 1,250 = 117,500; total 317,500;
                                   divisor = 300 x 317,500/300,000 = 317.5
        * 3-for-1 at 10, cum 100   TERP = (1x100 + 3x10)/4 = 32.5.
                                   Shares 1,000 x (1 + 3/1) = 4,000, so S0 is worth
                                   32.5 x 4,000 = 130,000; total 330,000;
                                   divisor = 300 x 330,000/300,000 = 330

        Both levels are 1,000.000000 - a rights issue is a divisor event, so the
        rebase is exactly what makes them equal. level error 0.000 bps.
        divisor error = |330/317.5 - 1| x 10,000 = (12.5/317.5) x 10,000
                      = 393.701 bps
        """
        state = make_state()
        truth = RightsIssue(
            **COMMON, subscription_price=70.0, new_shares=1, per_held=4, cum_price=100.0
        )
        predicted = RightsIssue(
            **COMMON, subscription_price=10.0, new_shares=3, per_held=1, cum_price=100.0
        )

        result = graded(impact_error(predicted, truth, state))

        assert result.level_error_bps == pytest.approx(0.0, abs=1e-9)
        assert result.truth_divisor == pytest.approx(317.5, abs=1e-9)
        assert result.predicted_divisor == pytest.approx(330.0, abs=1e-9)
        assert result.error_bps == pytest.approx(393.7008, abs=1e-3)

    def test_two_splits_with_different_ratios_are_genuinely_zero_impact(self) -> None:
        """The one case D-029 does NOT close, pinned deliberately.

        * Split(ratio=2.0)    price 100 -> 50, shares 1,000 -> 2,000
        * Split(ratio=10.0)   price 100 -> 10, shares 1,000 -> 10,000

        S0's market value is 100,000 in both cases - that is what "market value
        invariant" means, and `_apply_split` asserts it. Total market value stays
        300,000, `Split.is_divisor_event` is False so the divisor stays 300, and the
        level stays 1,000.000000. Both components are exactly zero and so is the
        headline.

        This is not a grader defect: the index impact of a wrong split ratio genuinely
        IS zero on the day, in level and in divisor alike, and reporting a non-zero bps
        would mean inventing a number the engine does not produce. But the error is not
        without consequence. `shares` is S in the index level formula and is persisted in
        the daily state file, carried forward unchanged by `_mark` at the next
        mark-to-market. A wrong split ratio therefore produces roughly 2,666.67 bps on
        this three-name fixture at the next day's refresh at the market's own price, and
        never self-corrects. The impact is deferred, not absent. The signal that this is
        an ungraded pass rather than a correct extraction is `identical_events`, which is
        why that field exists - a scoreboard reporting 0.00 bps with `identical_events=False`
        is reporting "no index impact on the ex-date", not "right answer".
        """
        state = make_state()
        truth = Split(**COMMON, ratio=2.0)
        predicted = Split(**COMMON, ratio=10.0)

        result = graded(impact_error(predicted, truth, state))

        assert result.truth_divisor == pytest.approx(300.0, abs=1e-9)
        assert result.predicted_divisor == pytest.approx(300.0, abs=1e-9)
        assert result.level_error_bps == pytest.approx(0.0, abs=1e-12)
        assert result.divisor_error_bps == pytest.approx(0.0, abs=1e-12)
        assert result.error_bps == pytest.approx(0.0, abs=1e-12)
        assert result.same_type is True
        assert result.identical_events is False, (
            "zero bps here must be distinguishable from a correct extraction"
        )

    def test_the_misclassification_penalty_is_weight_dependent_but_its_ratio_is_not(
        self,
    ) -> None:
        """The 67bp headline is a property of the three-name fixture. Hand-derived.

        For n constituents at 100.00 x 1,000 shares, market value MV = n x 100,000 and
        divisor MV/1,000. A 2.00 distribution on S0 removes a x S = 2,000 of market
        value. Write x = 2,000/MV. Then

            ReturnOfCapital  ->  level 1,000            (divisor rebased)
            CashDividend     ->  level 1,000 x (1 - x)  (divisor unchanged)
            misclassification error = x / (1 - x) x 10,000 bps

        and predicting 2.10 instead of 2.00 removes 1.05 x 2,000 instead, so

            misread-amount error = 0.05x / (1 - x) x 10,000 bps

        n = 1    x = 2,000/100,000    -> 2,000/98,000  x 10,000 = 204.0816 bps
        n = 10   x = 2,000/1,000,000  -> 2,000/998,000 x 10,000 =  20.0401 bps

        A 10x change in weight moves the headline by 10x. At a realistic 1-3%
        large-cap weight the same misclassification is 2-7 bps, which lands inside the
        "under 5bp for a misread amount" band the contrast is supposed to separate -
        so the two absolute numbers are not the claim.

        The ratio is: (x/(1-x)) / (0.05x/(1-x)) = 1/0.05 = 20 exactly, for every n,
        because the shared 1/(1-x) denominator and the shared x both cancel. That is
        weight-invariant and is what the eval actually argues.
        """
        errors = {}
        for n in (1, 10):
            misclassified = graded(
                impact_error(
                    ReturnOfCapital(**COMMON, amount=2.0),
                    CashDividend(**COMMON, amount=2.0),
                    make_state(n),
                )
            )
            misread = graded(
                impact_error(
                    CashDividend(**COMMON, amount=2.1),
                    CashDividend(**COMMON, amount=2.0),
                    make_state(n),
                )
            )
            errors[n] = (misclassified.error_bps, misread.error_bps)

        assert errors[1][0] == pytest.approx(204.0816, abs=1e-3)
        assert errors[10][0] == pytest.approx(20.0401, abs=1e-3)
        assert errors[1][0] / errors[10][0] == pytest.approx(10.1837, abs=1e-3)

        for n in (1, 10):
            assert errors[n][0] / errors[n][1] == pytest.approx(20.0, abs=1e-9), n

    def test_a_predicted_event_on_a_security_outside_the_index_is_ungraded(self) -> None:
        """`apply_event` returns the state untouched for a security it does not hold,
        so both sides come back with the starting level and a level-only grader scores
        a perfect 0.00 bps. Live path: `harvest_labels` takes `security_id` straight
        from vendor data ("AAPL") and nothing in this package builds a state containing
        those ids, so harvesting real labels and grading them against a fixture would
        produce an all-zeros scoreboard. See D-030."""
        result = impact_error(
            CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=2.0),
            CashDividend(**COMMON, amount=2.0),
            make_state(),
        )

        assert isinstance(result, Ungraded)
        assert "constituent" in result.reason
        assert "AAPL" in result.detail
        assert not hasattr(result, "error_bps")

    def test_a_truth_event_on_a_security_outside_the_index_is_ungraded(self) -> None:
        """The truth side is checked too. A corpus label on a name the fixture state
        does not hold is just as ungradable, and scoring it zero would flatter the
        model for a defect in the harness."""
        result = impact_error(
            CashDividend(**COMMON, amount=2.0),
            CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=2.0),
            make_state(),
        )

        assert isinstance(result, Ungraded)
        assert "constituent" in result.reason
        assert "truth" in result.detail

    def test_both_sides_off_index_is_ungraded_not_zero(self) -> None:
        """The exact shape that reported 0.0000 bps before D-030: neither event touches
        the state, so the two levels are identical and the two divisors are identical.
        Structurally indistinguishable from a perfect prediction."""
        off = {**COMMON, "security_id": "AAPL"}

        result = impact_error(
            ReturnOfCapital(**off, amount=2.0), CashDividend(**off, amount=2.0), make_state()
        )

        assert isinstance(result, Ungraded)

    @pytest.mark.parametrize(
        ("name", "event"),
        [
            ("split ratio zero", Split(**COMMON, ratio=0.0)),
            (
                "spinoff ratio zero",
                Spinoff(
                    **COMMON,
                    spinco_security_id="S1",
                    shares_per_parent_share=0.0,
                    value_per_parent_share=10.0,
                    parent_cum_price=100.0,
                ),
            ),
            (
                "rights entitlement zero",
                RightsIssue(
                    **COMMON,
                    subscription_price=70.0,
                    new_shares=1,
                    per_held=0,
                    cum_price=100.0,
                ),
            ),
        ],
    )
    def test_an_event_the_engine_rejects_is_ungraded_rather_than_raising(
        self, name: str, event: CorporateAction
    ) -> None:
        """The third occurrence of the defect already fixed twice on this branch: one
        malformed row propagating an exception out of a batch function and discarding
        every result computed before it. `Split(ratio=0.0)` raises `ZeroDivisionError`
        at `events.py:228`, `Spinoff(shares_per_parent_share=0.0)` raises `ValueError`
        at `events.py:428`, `RightsIssue(per_held=0)` raises `ValueError` at
        `events.py:320` - all three escaped `impact_error` uncaught. `build_event` now
        rejects all three at construction, but these are built directly here precisely
        because a prediction can reach the grader without passing through it. See
        D-032."""
        result = impact_error(event, CashDividend(**COMMON, amount=2.0), make_state())

        assert isinstance(result, Ungraded), name
        assert result.reason == "engine rejected the event"

    def test_a_bad_prediction_does_not_stop_the_next_pair_grading(self) -> None:
        """What the boundary is actually for. A caller grading a corpus in a loop must
        get a complete scoreboard with one row marked ungraded, not a traceback and
        nothing at all."""
        state = make_state()
        truth = CashDividend(**COMMON, amount=2.0)
        predictions: list[CorporateAction] = [
            Split(**COMMON, ratio=0.0),
            ReturnOfCapital(**COMMON, amount=2.0),
            CashDividend(**COMMON, amount=2.0),
        ]

        results = [impact_error(p, truth, state) for p in predictions]

        assert isinstance(results[0], Ungraded)
        assert graded(results[1]).error_bps == pytest.approx(67.114, abs=0.01)
        assert graded(results[2]).error_bps == pytest.approx(0.0, abs=1e-9)


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

        labels, skipped = harvest_labels(provider, ["AAPL"], D, D)

        assert skipped == []
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

        labels, skipped = harvest_labels(provider, ["AAPL"], D, D)

        assert len(labels) == 1
        assert labels[0].event.amount == 0.24
        assert len(skipped) == 1
        assert skipped[0].reason == "unbuildable"
        assert "amount" in skipped[0].detail

    def test_empty_frame_yields_nothing(self) -> None:
        assert harvest_labels(FakeActionsProvider([]), ["AAPL"], D, D) == ([], [])

    def test_reports_what_it_dropped_rather_than_returning_a_bare_list(self) -> None:
        """`join_labels_to_text` returns `(joined, unjoined)` and `extract_event`
        returns `abstained` plus a `reason`; this was the one fail-closed stage whose
        losses were invisible. With the drop count in hand, a harvest that lost most
        of its corpus is a number you can see - without it, a 98% smaller corpus still
        produces a scoreboard and the scoreboard still looks fine. See D-031."""
        provider = FakeActionsProvider(
            [
                _row("CASH_DIVIDEND", {"amount": 0.24}),
                _row("CASH_DIVIDEND", {}, ticker="MSFT"),
                _row("SPLIT", {"ratio": 0.0}, ticker="GOOG"),
                _row("NOT_AN_EVENT_TYPE", {"amount": 1.0}, ticker="NVDA"),
            ]
        )

        labels, skipped = harvest_labels(provider, ["AAPL"], D, D)

        assert len(labels) == 1
        assert len(skipped) == 3
        assert skip_counts(skipped) == {
            "unbuildable:CASH_DIVIDEND": 1,
            "unbuildable:SPLIT": 1,
            "unknown_event_type:NOT_AN_EVENT_TYPE": 1,
        }
        assert {row.security_id for row in skipped} == {"MSFT", "GOOG", "NVDA"}

    def test_a_vendor_payload_key_the_dataclass_does_not_declare_still_harvests(
        self,
    ) -> None:
        """The regression this pins, end to end. The repo's own `SyntheticUniverse`
        emits `gross_amount` on every dividend and `terp` on every rights issue -
        keys no handler declares. Before D-031 the first such row raised `TypeError`
        out of `build_event`, uncaught at `labels.py`, aborting the entire harvest and
        discarding every label already built. On the shipped synthetic universe that
        is 12,036 of 12,253 rows unbuildable (98.2%), of which 11,834 are the cash
        dividends this module exists to collect for free."""
        provider = FakeActionsProvider(
            [
                _row(
                    "CASH_DIVIDEND",
                    {"amount": 0.24, "currency": "USD", "gross_amount": 0.24, "is_special": False},
                ),
                _row(
                    "RIGHTS_ISSUE",
                    {
                        "new_shares": 1,
                        "per_held": 4,
                        "subscription_price": 70.0,
                        "cum_price": 100.0,
                        "terp": 94.0,
                        "currency": "USD",
                    },
                    ticker="MSFT",
                ),
                _row("SPLIT", {"ratio": 4.0}, ticker="GOOG"),
            ]
        )

        labels, skipped = harvest_labels(provider, ["AAPL", "MSFT", "GOOG"], D, D)

        assert skipped == []
        assert len(labels) == 3
        assert {label.event.event_type for label in labels} == {
            EventType.CASH_DIVIDEND,
            EventType.RIGHTS_ISSUE,
            EventType.SPLIT,
        }


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
        `announcement_id` disambiguates them because it embeds `raw_event_id`.

        **What `announcement_id` does not fix, and this test does not claim it does.**
        Distinct ids solve identity collision, not label contradiction. The two
        `Announcement`s below carry byte-identical `text` ("Board declares dividend.")
        and therefore an identical `provenance.sha256`, under contradictory labels: one
        says the filing announces a `CashDividend(0.24)`, the other that the same
        sentence announces a `Split(2.0)`. `extract_event` returns exactly one event per
        announcement, so **an extractor that is entirely correct is scored wrong on at
        least one of them**, and the resulting bps figure measures the join heuristic
        rather than the model. The window is a heuristic (D-025) and this is the shape
        of its worst case, made concrete: every label in the window is offered the same
        document, and nothing in `join_labels_to_text` notices that a one-sentence
        dividend notice cannot also be a split announcement. Deferred to stage 2 rather
        than fixed here - see D-025's Consequences for the disclosure and the options."""
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

    def test_an_unexpected_payload_field_is_ignored_rather_than_fatal(self) -> None:
        """CHANGED by D-031. The system prompt names the permitted `event_type` values
        but never the payload schema for each one, so nothing stops a model adding a
        key the dataclass does not declare (a stray `confidence` field). D-027 made
        that abstain, by widening `extract_event`'s `except` to catch the
        `TypeError` the dataclass constructor raises.

        Abstaining is the wrong trade once the same code path is measured on real
        vendor data rather than on model output: the repo's own synthetic universe
        emits an undeclared `gross_amount` on every dividend, and abstaining on an
        undeclared key discards 98% of the corpus to protect against a field that,
        by construction, describes nothing the taxonomy models. `build_event` now
        filters the payload to the handler's declared fields, so the extraction
        succeeds and the extra key is dropped. Everything in `spec.required` is still
        checked for presence, type and range - the two tests below this one still
        abstain."""
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

        assert result.abstained is False
        assert isinstance(result.event, CashDividend)
        assert result.event.amount == 2.0
        assert not hasattr(result.event, "confidence")

    def test_a_non_object_payload_abstains_rather_than_raising(self) -> None:
        """`parsed.get("payload", {})` is handed to `build_event` unchecked, and a
        model can emit a JSON list there as easily as an object. Filtering a list by
        declared field names would raise `AttributeError`, which nothing catches;
        `build_event`'s payload guard turns it into the `TaxonomyError` this
        `except` clause already handles."""
        client = ScriptedLlm(
            _json.dumps(
                {
                    "event_type": "CASH_DIVIDEND",
                    "ex_date": "2024-06-10",
                    "announcement_date": "2024-05-20",
                    "pay_date": "2024-06-24",
                    "payload": [1, 2, 3],
                }
            )
        )

        result = extract_event(client, "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None

    def test_a_payload_identifier_cannot_move_the_grade_to_another_security(self) -> None:
        """The model is asked about `S0` and answers about `S1`.

        All five `COMMON_FIELDS` are declared fields on every handler dataclass, so the
        declared-field filter D-031 added let them through and `kwargs.update(payload)`
        gave the payload the last word. `extract_event` then returned a non-abstained
        `CashDividend` on `S1` carrying a hallucinated `event_id` - and `impact_error`
        graded it, against a different constituent's index weight.

        Both branches score the model for something other than what it did, and this
        test pins the worse-looking one: `S1` is not in a one-name state, so the wrong
        security made `impact_error` return `Ungraded`, **silently deleting a wrong
        extraction from the scoreboard** rather than charging the model for it.

        Hand-derived, one name at 100.00 x 1,000 shares -> market value 100,000, base
        level 1,000 -> divisor 100. `CashDividend` is not a divisor event, so the
        divisor stays at 100 on both sides:

            truth     1.00/share -> price 99.00, MV 99,000, level 990.000000
            predicted 2.00/share -> price 98.00, MV 98,000, level 980.000000
            level error = |980 - 990| / 990 x 10,000 = 101.0101 bps

        See D-033."""
        client = ScriptedLlm(
            _json.dumps(
                {
                    "event_type": "CASH_DIVIDEND",
                    "ex_date": "2024-06-10",
                    "announcement_date": "2024-05-20",
                    "pay_date": "2024-06-24",
                    "payload": {
                        "amount": 2.0,
                        "security_id": "S1",
                        "event_id": "HALLUCINATED",
                    },
                }
            )
        )

        result = extract_event(client, "…dividend of $2.00…", "S0", event_id="E1")

        assert result.abstained is False
        assert isinstance(result.event, CashDividend)
        assert result.event.security_id == "S0", "the caller's security, not the model's"
        assert result.event.event_id == "E1", "the caller's event id, not the model's"

        state = make_state(n=1)  # holds S0 only; S1 would be ungradable
        truth = CashDividend(**COMMON, amount=1.0)

        graded_result = graded(impact_error(result.event, truth, state))

        assert graded_result.error_bps == pytest.approx(101.0101, abs=0.001)
        assert graded_result.identical_events is False

    def test_a_zero_split_ratio_abstains_rather_than_extracting(self) -> None:
        """`0.0` is a valid `float`, so before D-032 this returned a NON-abstained
        `Split(ratio=0.0)` - a confident extraction that raises `ZeroDivisionError`
        the moment the grader applies it."""
        client = ScriptedLlm(
            _json.dumps(
                {
                    "event_type": "SPLIT",
                    "ex_date": "2024-06-10",
                    "announcement_date": "2024-05-20",
                    "pay_date": "2024-06-24",
                    "payload": {"ratio": 0.0},
                }
            )
        )

        result = extract_event(client, "…", "S0", event_id="E1")

        assert result.abstained is True
        assert result.event is None
        assert "ratio" in result.reason


class EchoingLlm(LlmClient):
    """Returns the announcement text back verbatim, as the model's whole answer.

    The end-to-end test writes each filing's body as the JSON a correct model would
    emit, so echoing the prompt back is a perfectly correct extractor - which is the
    point. A stub that returns a fixed body (`ScriptedLlm`) can only ever exercise one
    row; this one is scored against every label in the slice, so anything the pipeline
    does to a payload on its way to an event shows up as a non-zero bps figure.

    That is not hypothetical. The payloads this stub echoes carry a `security_id` and an
    `event_id` belonging to a *different* label - the shape of an 8-K that names two
    issuers, and of a model that copies the wrong identifier out of the prose. Before
    D-033, `build_event` let a payload overwrite both, so all 31 rows extracted onto the
    wrong constituent and all 31 misgraded; a payload-echoing stub would have caught it
    the first time anyone wrote one.
    """

    name = "echoing"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(
        self,
        messages: list[Message],
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse:
        prompt = messages[-1].content
        self.prompts.append(prompt)
        return LlmResponse(text=prompt.split("Announcement:\n", 1)[1], model="echoing")


def _model_shaped_filing(label: LabelledEvent, other_security_id: str) -> str:
    """The JSON a correct model would return for `label`, used as a filing body.

    Every declared field beyond `COMMON_FIELDS` goes into the payload, so a correct
    extraction is field-for-field identical to the label and `identical_events` is
    `True` - a thinner payload would score zero bps for the uninteresting reason that
    the omitted fields happen to equal their defaults. `security_id` and `event_id` are
    then set to *another* label's, which the taxonomy must ignore. See D-033.
    """
    event = label.event
    payload: dict[str, Any] = {
        field.name: getattr(event, field.name)
        for field in fields(cast(Any, event))
        if field.name not in COMMON_FIELDS
    }
    payload["security_id"] = other_security_id
    payload["event_id"] = f"WRONG-{label.raw_event_id}"
    return _json.dumps(
        {
            "event_type": str(event.event_type),
            "ex_date": event.ex_date.isoformat(),
            "announcement_date": event.announcement_date.isoformat(),
            "pay_date": event.pay_date.isoformat(),
            "payload": payload,
        }
    )


def _state_from_provider(universe: SyntheticUniverse, as_of: dt.date) -> IndexState:
    """An `IndexState` on `as_of` built from the provider's own prices and share counts.

    The wiring D-030 observes that nothing in `triage/` performs, done here so the
    end-to-end test grades harvested labels against a state that actually holds their
    securities rather than against `make_state`'s hand-built `S0`/`S1`/`S2`. Names
    without both a price and a share count on the date (not yet listed, delisted,
    suspended) are simply not constituents, which is why the test filters its label
    slice to `state.constituents` rather than assuming every harvested name is indexed.
    """
    prices = universe.get_prices(None, as_of, as_of)
    shares = universe.get_shares(None, as_of)
    shares_by_id = {
        str(row.security_id): float(row.shares_outstanding) for row in shares.itertuples()
    }
    constituents = {
        str(row.security_id): Constituent(
            security_id=str(row.security_id),
            price=float(row.close),
            shares=shares_by_id[str(row.security_id)],
        )
        for row in prices.itertuples()
        if str(row.security_id) in shares_by_id
    }
    assert constituents, "no priced securities on this date"
    return IndexState.initialise(as_of, constituents, base_level=1000.0)


class TestEndToEnd:
    """harvest -> join -> extract -> verify, on a slice of the repo's own universe.

    Every other test in this file exercises one stage against hand-built inputs. The
    branch's ledger names the absence of this walk "the structural reason C2/I1/I5
    survived six scoped reviews": each stage was correct against the fixtures written
    for it, and the defects lived in what one stage handed the next. Three of them - an
    off-index `security_id` scoring 0.0 bps, an undeclared payload key aborting the
    harvest, and a payload overwriting the caller's `security_id` - are invisible to any
    test that does not run the stages in series against real vendor rows. See D-035.
    """

    def test_a_correct_extractor_scores_zero_bps_across_the_pipeline(self) -> None:
        """A twelve-name synthetic universe over eighteen months, graded end to end.

        The claim under test is not "nothing raised". It is that a **perfectly correct
        extractor scores exactly 0.00 bps on every stage-1 label in the slice, with
        nothing lost at any stage**: no skipped vendor row, no unjoined label, no
        abstention, and no `Ungraded` pair. That is the only baseline against which a
        real model's bps distribution means anything - if the harness itself loses rows
        or misgrades a correct answer, every number measured through it is a number
        about the harness.

        Each assertion pins a stage that has actually failed on this branch:

        * `skipped == []` - one undeclared vendor key (`gross_amount`) made 98.2% of
          this exact universe unbuildable, and aborted the batch as well (D-031).
        * `unjoined == []` - the join fails closed, so a silent shrink shows up as a
          smaller corpus rather than as an error (D-025).
        * `security_id` - the extracted event must be about the security the
          announcement is about, not one a payload named (D-033).
        * `ImpactError`, never `Ungraded` - an event on a name the state does not hold
          is absent from a scoreboard rather than scored (D-030).
        * `error_bps == 0` with `identical_events` - a correct extraction is worth
          exactly zero, and zero-because-correct must stay distinguishable from
          zero-because-unmeasurable (D-029).

        Twelve names over eighteen months is the smallest slice that still yields both
        stage-1 classes (`CashDividend` and `Split`) from the generator's own event
        intensities; the whole walk runs in about 0.1s.
        """
        universe = SyntheticUniverse(
            SyntheticConfig(
                n_securities=12,
                seed=20260809,
                start=dt.date(2022, 1, 1),
                end=dt.date(2023, 6, 30),
            )
        )
        state = _state_from_provider(universe, dt.date(2022, 3, 1))

        labels, skipped = harvest_labels(
            universe,
            list(universe.get_securities()["security_id"]),
            dt.date(2022, 1, 1),
            dt.date(2023, 6, 30),
        )

        assert skipped == [], f"vendor rows lost before grading began: {skip_counts(skipped)}"

        in_scope = [
            label
            for label in labels
            if label.security_id in state.constituents
            and TAXONOMY[label.event.event_type].in_scope_stage == 1
        ]
        assert len(in_scope) >= 10, "slice too small to be worth walking"
        assert {type(label.event).__name__ for label in in_scope} == {"CashDividend", "Split"}

        securities = [label.security_id for label in in_scope]
        documents = [
            FilingDocument(
                accession=f"ACC-{i:04d}",
                filed=label.event.announcement_date,
                url=f"https://www.sec.gov/Archives/ACC-{i:04d}",
                text=_model_shaped_filing(label, securities[(i + 1) % len(securities)]),
                security_id=label.security_id,
            )
            for i, label in enumerate(in_scope)
        ]

        joined, unjoined = join_labels_to_text(
            in_scope, documents, window_days=5, retrieved=dt.date(2022, 3, 1)
        )

        assert unjoined == []
        assert len(joined) == len(in_scope)

        client = EchoingLlm()
        results: list[ImpactError] = []
        for announcement, label in zip(joined, in_scope, strict=True):
            extraction = extract_event(
                client,
                announcement.text,
                announcement.security_id,
                event_id=label.raw_event_id,
            )

            assert extraction.abstained is False, extraction.reason
            assert extraction.event is not None
            assert extraction.event.security_id == announcement.security_id
            assert extraction.event.event_id == label.raw_event_id

            results.append(graded(impact_error(extraction.event, label.event, state)))

        assert len(results) == len(in_scope)
        assert all(result.error_bps == pytest.approx(0.0, abs=1e-9) for result in results)
        assert all(result.identical_events for result in results)
        assert all(result.same_type for result in results)
        assert len(client.prompts) == len(in_scope)
