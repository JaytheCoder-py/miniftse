# Corporate Action Triage — Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic grading spine for corporate action triage — a pinned label space, a basis-point impact grader, a labelled announcement corpus, and a first LLM extraction pass on the easy classes.

**Architecture:** A new `miniftse.triage` package. `taxonomy.py` pins the 16 `EventType` values to their handler classes and required parameters. `verify.py` grades a predicted event against a true one by applying both through the existing `CorporateActionEngine` and diffing the resulting index level in basis points. `corpus.py` holds announcements with provenance and labels. `labels.py` harvests free labels from yfinance; `text.py` fetches filing text from SEC and joins it to those labels. `extract.py` is the LLM layer, and it is deliberately last — everything before it is deterministic and testable without a key.

**Tech Stack:** Python 3.12+, `uv`, `pandas`, `pytest`, `mypy --strict`, `ruff`. Existing `miniftse.corpactions`, `miniftse.calc.state`, `miniftse.agents.llm`.

## Global Constraints

- Python `>=3.12`; the repo tests on 3.12 and 3.13.
- `ruff check src tests` and `ruff format --check src tests` must pass.
- `mypy src/miniftse` must pass; the core is `--strict`.
- No index arithmetic outside `miniftse.calc` and `miniftse.corpactions`. `verify.py` wraps the engine; it never recomputes a level.
- **No number in any output originates from a language model.** Rule 1 of `agents/llm.py`.
- Nothing in `triage/` may be imported by the index calculation path.
- Tests use hand-computed expected values, per `tests/test_index_maths.py`. A test asserting the code agrees with itself proves nothing.
- Test style: `class TestX:` with `def test_y(self) -> None:` methods. Full type annotations.
- No network in tests. Network-touching code takes an injected provider.
- Every judgement call gets a `DECISIONS.md` entry with the alternative rejected.
- Commit after each task.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/miniftse/triage/__init__.py` | Package exports |
| `src/miniftse/triage/taxonomy.py` | `EventType` → handler class → required fields; event construction from a payload |
| `src/miniftse/triage/verify.py` | Impact error in bps between a predicted and a true event |
| `src/miniftse/triage/corpus.py` | `Announcement`, `Provenance`, JSONL persistence |
| `src/miniftse/triage/labels.py` | Free labels harvested from a market data provider |
| `src/miniftse/triage/text.py` | SEC filing text acquisition and the label join |
| `src/miniftse/triage/extract.py` | LLM structured extraction (stretch) |
| `tests/test_triage.py` | All of the above |

---

### Task 1: Taxonomy — pin the label space

The model cannot be graded against an ambiguous label space. `EventType` has 16 values; `engine.apply_event` dispatches on 10 concrete classes. This task records the mapping and proves it is total.

**Files:**
- Create: `src/miniftse/triage/__init__.py`
- Create: `src/miniftse/triage/taxonomy.py`
- Create: `tests/test_triage.py`

**Interfaces:**
- Consumes: `miniftse.corpactions.events.{EventType, CorporateAction, CashDividend, ReturnOfCapital, Split, RightsIssue, Spinoff, CashMerger, StockMerger, Delisting, SharesChange, FloatChange}`
- Produces: `EventSpec`, `TAXONOMY: dict[EventType, EventSpec]`, `build_event(event_type, common, payload) -> CorporateAction`

- [ ] **Step 1: Confirm the handler dispatch list**

Read `src/miniftse/corpactions/engine.py:112-123`. That dict is authoritative for which classes have handlers. Read `src/miniftse/corpactions/events.py:31-47` for the 16 `EventType` values, and each class's `event_type` property to see which types share a class.

Three types have no confirmed concrete handler: `STOCK_DIVIDEND`, `TENDER_OFFER`, `SUSPENSION`. Grep for them:

```bash
grep -rn "STOCK_DIVIDEND\|TENDER_OFFER\|SUSPENSION" src/miniftse/
```

Record what you find. If a type has no handler, it gets `handler=None` in the taxonomy — that is a finding, not a gap.

- [ ] **Step 2: Write the failing test**

Create `tests/test_triage.py`:

```python
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
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `uv run pytest tests/test_triage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'miniftse.triage'`

- [ ] **Step 4: Create the package**

Create `src/miniftse/triage/__init__.py`:

```python
"""Corporate action triage: read an announcement, produce the structured event.

Graded by index impact in basis points rather than classification accuracy. A misread
dividend amount is nearly free; a spin-off booked as a special dividend breaks divisor
continuity and moves the published level. Accuracy scores those the same. Basis points
do not.

Nothing here is imported by the calculation path.
"""
```

- [ ] **Step 5: Write the taxonomy**

Create `src/miniftse/triage/taxonomy.py`. Fill `TAXONOMY` from what Step 1 found — the entries below are confirmed; complete the remainder the same way.

```python
"""The label space, pinned.

`EventType` has 16 values; `CorporateActionEngine.apply_event` dispatches on 10 concrete
classes. Several types share a class and are distinguished by a field - a special
dividend is a `CashDividend` with `is_special=True`, a reverse split is a `Split` with
`ratio < 1`. A model cannot be graded against a label space that is not written down,
so this module writes it down and `test_covers_every_event_type` keeps it total.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Final

from miniftse.corpactions.events import (
    CashDividend,
    CashMerger,
    CorporateAction,
    Delisting,
    EventType,
    FloatChange,
    ReturnOfCapital,
    RightsIssue,
    SharesChange,
    Spinoff,
    Split,
    StockMerger,
)

COMMON_FIELDS: Final[tuple[str, ...]] = (
    "event_id",
    "security_id",
    "ex_date",
    "announcement_date",
    "pay_date",
)


@dataclass(frozen=True, slots=True)
class EventSpec:
    """One row of the label space."""

    handler: type[CorporateAction] | None
    required: tuple[str, ...]
    """Fields beyond COMMON_FIELDS that the handler needs."""

    defaults: dict[str, Any]
    """Fields the handler needs that are implied by the event type rather than read
    from the announcement - `is_special=True` for a special dividend."""

    in_scope_stage: int | None
    """Stage at which this type enters the eval set. None means unmapped."""

    note: str = ""


TAXONOMY: Final[dict[EventType, EventSpec]] = {
    EventType.CASH_DIVIDEND: EventSpec(
        CashDividend, ("amount",), {"is_special": False}, 1
    ),
    EventType.SPECIAL_DIVIDEND: EventSpec(
        CashDividend, ("amount",), {"is_special": True}, 3,
        note="Same class as CASH_DIVIDEND. The line between them is a published "
             "materiality threshold, which is exactly why it is a stage-3 class.",
    ),
    EventType.RETURN_OF_CAPITAL: EventSpec(ReturnOfCapital, ("amount",), {}, 3,
        note="Identical price effect to a cash dividend, opposite divisor treatment. "
             "The canonical misclassification.",
    ),
    EventType.SPLIT: EventSpec(Split, ("ratio",), {}, 1),
    EventType.REVERSE_SPLIT: EventSpec(Split, ("ratio",), {}, 1,
        note="Same class; Split.event_type returns REVERSE_SPLIT when ratio < 1.",
    ),
    EventType.BONUS_ISSUE: EventSpec(Split, ("ratio",), {}, 3,
        note="Arithmetically identical to a split - see the Split docstring.",
    ),
    EventType.RIGHTS_ISSUE: EventSpec(
        RightsIssue,
        ("subscription_price", "new_shares", "per_held", "cum_price"),
        {}, 3,
        note="TERP needs the full terms - a rights issue cannot be summarised as one "
             "ratio. `new_shares`/`per_held` is the entitlement (2-for-5 is 2 and 5).",
    ),
    EventType.SPINOFF: EventSpec(
        Spinoff,
        ("spinco_security_id", "shares_per_parent_share", "value_per_parent_share",
         "parent_cum_price"),
        {}, 3,
    ),
    EventType.MERGER_CASH: EventSpec(CashMerger, ("cash_per_share",), {}, 3,
        note="`cash_per_share` shadows the base class property of the same name; see "
             "the type: ignore on the dataclass field.",
    ),
    EventType.MERGER_STOCK: EventSpec(
        StockMerger,
        ("acquirer_security_id", "exchange_ratio", "implied_value_per_share"),
        {}, 3,
    ),
    EventType.DELISTING: EventSpec(Delisting, (), {}, 3,
        note="`final_price` and `reason` both default. `final_price` defaulting to 0.0 "
             "means a delisting extracted without one writes the position to zero - "
             "consider promoting it to required before stage 3 admits this class.",
    ),
    EventType.SHARES_CHANGE: EventSpec(SharesChange, ("new_shares", "old_shares"), {}, 3),
    EventType.FLOAT_CHANGE: EventSpec(FloatChange, ("new_float", "old_float"), {}, 3),
    # Complete STOCK_DIVIDEND, TENDER_OFFER and SUSPENSION from Step 1's findings.
    # If a type has no handler: EventSpec(None, (), {}, None, note="<what you found>").
}


class TaxonomyError(ValueError):
    """The requested event cannot be constructed."""


def build_event(
    event_type: EventType,
    common: dict[str, Any],
    payload: dict[str, Any],
) -> CorporateAction:
    """Construct a `CorporateAction` from a type and a flat payload.

    Raises rather than guessing. A silently-defaulted amount produces an event that
    applies cleanly and grades wrong, which is the worst possible failure here.
    """
    spec = TAXONOMY[event_type]
    if spec.handler is None:
        raise TaxonomyError(f"no handler for {event_type}: {spec.note}")

    missing_common = [f for f in COMMON_FIELDS if f not in common]
    if missing_common:
        raise TaxonomyError(f"missing common fields: {', '.join(missing_common)}")

    missing = [f for f in spec.required if f not in payload]
    if missing:
        raise TaxonomyError(f"{event_type} requires {', '.join(missing)}")

    kwargs: dict[str, Any] = dict(common)
    kwargs.update({f: payload[f] for f in spec.required})
    kwargs.update(spec.defaults)
    return spec.handler(**kwargs)
```

**The `required` field names above were verified against the dataclasses on 2026-08-13**
by reading each class in `src/miniftse/corpactions/events.py`. Do not take them on trust
if that file has changed since — a wrong name here builds a valid-looking `EventSpec`
whose `build_event` raises only when stage 3 first admits that class.

Worth noting what the verification found, because it is the point of doing it: every one
of these names was initially guessed wrong. A rights issue does not reduce to a single
`ratio`, a spin-off needs four fields rather than one, and `FloatChange` takes
`new_float`/`old_float` rather than a factor. The event model is more demanding than a
classification taxonomy suggests, and that difficulty is exactly what the eval measures.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_triage.py -v`
Expected: PASS. If `test_covers_every_event_type` fails, `TAXONOMY` is missing an entry — add it rather than relaxing the test.

- [ ] **Step 7: Lint and typecheck**

Run: `uv run ruff check src tests && uv run ruff format src tests && uv run mypy src/miniftse/triage`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/miniftse/triage/ tests/test_triage.py
git commit -m "triage: pin the corporate action label space"
```

---

### Task 2: Verify — impact error in basis points

The grader. This is the task that makes the whole project different from a classification demo.

**Files:**
- Create: `src/miniftse/triage/verify.py`
- Modify: `tests/test_triage.py` (append)

**Interfaces:**
- Consumes: `miniftse.corpactions.engine.CorporateActionEngine`, `miniftse.calc.state.{Constituent, IndexState}`, `taxonomy` (nothing directly)
- Produces: `ImpactError` (fields: `predicted_level`, `truth_level`, `error_bps`, `predicted_divisor`, `truth_divisor`, `same_type`), `impact_error(predicted, truth, state, *, withholding_tax=None) -> ImpactError`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_triage.py`. **The expected value is hand-computed** — the working is in the docstring so a reviewer can check it with a calculator.

```python
from miniftse.calc.state import Constituent, IndexState
from miniftse.corpactions.events import ReturnOfCapital
from miniftse.triage.verify import impact_error


def make_state(n: int = 3, price: float = 100.0, shares: float = 1000.0) -> IndexState:
    constituents = {
        f"S{i}": Constituent(f"S{i}", price=price, shares=shares) for i in range(n)
    }
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_triage.py::TestImpactError -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'miniftse.triage.verify'`

- [ ] **Step 3: Implement**

Create `src/miniftse/triage/verify.py`:

```python
"""Grading an extracted event by what it would do to the published index.

Classification accuracy is the wrong metric and using it would be the obvious mistake.
A misread dividend amount is nearly free. A spin-off booked as a special dividend
breaks divisor continuity and moves the level. Accuracy scores those identically.

So: apply the predicted event and the true event to the same state, and report the
difference between the resulting index levels in basis points. That is the number the
business already uses, and it makes the eval a risk measure rather than a proxy for one.

This module computes no index arithmetic of its own. It calls the engine twice and
subtracts, which keeps one source of truth for every published figure - the same
constraint `test_desk_contains_no_index_arithmetic` enforces on the desk.
"""

from __future__ import annotations

from dataclasses import dataclass

from miniftse.calc.state import IndexState
from miniftse.corpactions.engine import CorporateActionEngine
from miniftse.corpactions.events import CorporateAction


@dataclass(frozen=True, slots=True)
class ImpactError:
    """What getting this event wrong would have cost the published level."""

    predicted_level: float
    truth_level: float
    error_bps: float
    predicted_divisor: float
    truth_divisor: float
    same_type: bool
    """Diagnostic only. A correct type with a wrong parameter and an incorrect type
    are different failures and are reported separately."""


def _level_after(
    event: CorporateAction,
    state: IndexState,
    withholding_tax: dict[str, float] | None,
) -> tuple[float, float]:
    engine = CorporateActionEngine(withholding_tax=dict(withholding_tax or {}))
    result = engine.apply_event(event, state)
    return result.state.level, result.state.divisor


def impact_error(
    predicted: CorporateAction,
    truth: CorporateAction,
    state: IndexState,
    *,
    withholding_tax: dict[str, float] | None = None,
) -> ImpactError:
    """Basis points of index level between a predicted event and the true one.

    A fresh engine per application: the engine accumulates an audit trail, and grading
    must not leave one behind.
    """
    predicted_level, predicted_divisor = _level_after(predicted, state, withholding_tax)
    truth_level, truth_divisor = _level_after(truth, state, withholding_tax)

    if truth_level == 0.0:
        raise ValueError("truth level is zero; cannot express an error in basis points")

    error_bps = abs(predicted_level - truth_level) / abs(truth_level) * 10_000.0

    return ImpactError(
        predicted_level=predicted_level,
        truth_level=truth_level,
        error_bps=error_bps,
        predicted_divisor=predicted_divisor,
        truth_divisor=truth_divisor,
        same_type=predicted.event_type is truth.event_type,
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_triage.py -v`
Expected: PASS, including the 67.114 bps assertion.

If the number differs, **do not adjust the assertion**. Re-derive it by hand from the docstring's working. A mismatch means either the engine's rebase semantics differ from the docstring or the fixture is wrong, and both are worth knowing.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run mypy src/miniftse/triage
git add src/miniftse/triage/verify.py tests/test_triage.py
git commit -m "triage: grade extracted events in basis points of index impact"
```

- [ ] **Step 6: Record the decision**

Append to `DECISIONS.md`:

```markdown
## D-022: Triage is graded in basis points, not classification accuracy

Accuracy weights a misread dividend amount the same as a return of capital booked as
an ordinary dividend. On a three-name fixture the second costs 67bp of index level and
the first costs under 5bp. Reporting one number that cannot tell them apart would make
the eval actively misleading.

**Rejected:** F1 over event types, which is the default for a classification task and
is what a reviewer will expect. It is retained as a secondary diagnostic (`same_type`)
because it localises *where* the error is, but it is not the headline.
```

---

### Task 3: Corpus — announcements with provenance

**Files:**
- Create: `src/miniftse/triage/corpus.py`
- Modify: `tests/test_triage.py` (append)

**Interfaces:**
- Consumes: `taxonomy.{TAXONOMY, build_event, COMMON_FIELDS}`
- Produces: `Provenance`, `LabelSource`, `Announcement`, `write_jsonl(path, announcements)`, `read_jsonl(path) -> list[Announcement]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_triage.py`:

```python
from pathlib import Path

from miniftse.triage.corpus import (
    Announcement,
    LabelSource,
    Provenance,
    read_jsonl,
    write_jsonl,
)

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
            Announcement(
                announcement_id="A3", security_id="S0", text="  ", provenance=PROV
            )
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_triage.py::TestCorpus -v`
Expected: FAIL — no module `miniftse.triage.corpus`.

- [ ] **Step 3: Implement**

Create `src/miniftse/triage/corpus.py`:

```python
"""The labelled announcement corpus.

JSONL rather than a database: the corpus is small, it wants to be diffable in review,
and a labelling disagreement should show up in `git diff` as one changed line.

Provenance is mandatory on every row. An announcement whose source cannot be checked
cannot be used to argue that the model got it right.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from miniftse.corpactions.events import CorporateAction, EventType
from miniftse.triage.taxonomy import COMMON_FIELDS, TAXONOMY, build_event


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

        spec = TAXONOMY[self.label.event_type]
        common = {f: getattr(self.label, f) for f in COMMON_FIELDS}
        row["label"] = {
            "event_type": str(self.label.event_type),
            "common": {
                k: v.isoformat() if isinstance(v, dt.date) else v
                for k, v in common.items()
            },
            "payload": {f: getattr(self.label, f) for f in spec.required},
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
            label = build_event(
                EventType(raw_label["event_type"]), common, raw_label["payload"]
            )
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
```

**Note:** `ex_date` in `COMMON_FIELDS` ends in `_date`, as do `announcement_date` and
`pay_date`, so the `from_dict` date reconstruction covers all three. `event_id` and
`security_id` are strings and pass through. Confirm that holds if you add a common field.

- [ ] **Step 4: Run, lint, typecheck**

Run: `uv run pytest tests/test_triage.py -v && uv run ruff check src tests && uv run mypy src/miniftse/triage`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/miniftse/triage/corpus.py tests/test_triage.py
git commit -m "triage: labelled announcement corpus with mandatory provenance"
```

---

### Task 4: Free labels from a market data provider

`YFinanceProvider.get_corp_actions` already returns `event_id`, `security_id`,
`event_type`, the three dates and a JSON `payload` — the exact shape `build_event` wants.
**It returns no announcement text**, which is why this task produces labelled *events*
and Task 5 joins them to text.

**Files:**
- Create: `src/miniftse/triage/labels.py`
- Modify: `tests/test_triage.py` (append)

**Interfaces:**
- Consumes: `taxonomy.build_event`, a provider exposing `get_corp_actions(security_ids, start, end) -> pd.DataFrame`
- Produces: `LabelledEvent` (fields: `event`, `security_id`, `source`, `raw_event_id`), `harvest_labels(provider, security_ids, start, end) -> list[LabelledEvent]`

- [ ] **Step 1: Write the failing test with a fake provider**

Append to `tests/test_triage.py`. No network — the provider is injected.

```python
import json as _json

import pandas as pd

from miniftse.triage.labels import LabelledEvent, harvest_labels


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
        provider = FakeActionsProvider([
            _row("CASH_DIVIDEND", {"amount": 0.24}),
            _row("SPLIT", {"ratio": 4.0}),
        ])

        labels = harvest_labels(provider, ["AAPL"], D, D)

        assert len(labels) == 2
        assert {label.event.event_type for label in labels} == {
            EventType.CASH_DIVIDEND,
            EventType.SPLIT,
        }
        assert all(isinstance(label, LabelledEvent) for label in labels)

    def test_skips_a_row_it_cannot_build_rather_than_guessing(self) -> None:
        provider = FakeActionsProvider([
            _row("CASH_DIVIDEND", {}),              # no amount
            _row("CASH_DIVIDEND", {"amount": 0.24}),
        ])

        labels = harvest_labels(provider, ["AAPL"], D, D)

        assert len(labels) == 1
        assert labels[0].event.amount == 0.24

    def test_empty_frame_yields_nothing(self) -> None:
        assert harvest_labels(FakeActionsProvider([]), ["AAPL"], D, D) == []
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_triage.py::TestFreeLabels -v`
Expected: FAIL — no module `miniftse.triage.labels`.

- [ ] **Step 3: Implement**

Create `src/miniftse/triage/labels.py`:

```python
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
```

- [ ] **Step 4: Run, lint, typecheck, commit**

```bash
uv run pytest tests/test_triage.py -v
uv run ruff check src tests && uv run ruff format src tests && uv run mypy src/miniftse/triage
git add src/miniftse/triage/labels.py tests/test_triage.py
git commit -m "triage: harvest free labels from vendor corporate actions"
```

---

### Task 5: Filing text, and the join

The task the spec got wrong and this plan corrects. Labels come from vendor data; text
comes from SEC filings; joining them is fuzzy and must fail closed.

**Files:**
- Create: `src/miniftse/triage/text.py`
- Modify: `tests/test_triage.py` (append)

**Interfaces:**
- Consumes: `labels.LabelledEvent`, `corpus.{Announcement, Provenance, LabelSource}`
- Produces: `FilingDocument` (fields: `accession`, `filed`, `url`, `text`, `security_id`), `join_labels_to_text(labels, documents, *, window_days=5, retrieved=None) -> tuple[list[Announcement], list[LabelledEvent]]` returning `(joined, unjoined)`, and `fetch_filings(cik, security_id, contact, *, forms, since, limit) -> list[FilingDocument]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_triage.py`:

```python
from miniftse.triage.text import FilingDocument, join_labels_to_text


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
        document = _doc(dt.date(2024, 5, 19))   # announcement_date is 2024-05-20

        joined, unjoined = join_labels_to_text([label], [document], window_days=5)

        assert len(joined) == 1
        assert unjoined == []
        assert joined[0].label_source is LabelSource.AUTO
        assert joined[0].text == "Board declares dividend."
        assert joined[0].provenance.source == "sec-edgar"

    def test_drops_a_label_with_no_filing_in_the_window(self) -> None:
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        document = _doc(dt.date(2024, 1, 2))    # far outside the window

        joined, unjoined = join_labels_to_text([label], [document], window_days=5)

        assert joined == []
        assert len(unjoined) == 1

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
        label = LabelledEvent(
            event=CashDividend(**{**COMMON, "security_id": "AAPL"}, amount=0.24),
            security_id="AAPL",
            source="vendor-actions",
            raw_event_id="YF-1",
        )
        other = FilingDocument(
            accession="X", filed=dt.date(2024, 5, 20),
            url="https://www.sec.gov/Archives/X", text="MSFT news", security_id="MSFT",
        )

        joined, unjoined = join_labels_to_text([label], [other], window_days=5)

        assert joined == []
        assert len(unjoined) == 1
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_triage.py::TestTextJoin -v`
Expected: FAIL — no module `miniftse.triage.text`.

- [ ] **Step 3: Implement the join (pure, no network)**

Create `src/miniftse/triage/text.py`:

```python
"""Filing text, and joining it to vendor labels.

The join is the honest problem in this stage. Vendor data says *a dividend of 0.24 went
ex on 2024-06-10*. A filing says *the Board declared a quarterly dividend*. Nothing
links them but the issuer and a date, so the join is a heuristic and is treated as one:
nearest filing within a window, same security, and **anything unjoined is dropped and
counted** rather than paired with a plausible neighbour.

A wrongly joined pair is worse than a dropped one. It puts the right label on the wrong
text, and every metric computed downstream inherits the error silently.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass

from miniftse.triage.corpus import Announcement, LabelSource, Provenance
from miniftse.triage.labels import LabelledEvent


@dataclass(frozen=True, slots=True)
class FilingDocument:
    accession: str
    filed: dt.date
    url: str
    text: str
    security_id: str


def join_labels_to_text(
    labels: list[LabelledEvent],
    documents: list[FilingDocument],
    *,
    window_days: int = 5,
    retrieved: dt.date | None = None,
) -> tuple[list[Announcement], list[LabelledEvent]]:
    """Pair each label with the nearest filing for the same security.

    Returns `(joined, unjoined)`. The second element is not a failure to hide - the
    unjoined count is a reported property of the corpus, because a corpus that silently
    kept only the easy-to-join announcements would be biased in a way no metric reveals.
    """
    retrieved = retrieved or dt.date.today()
    by_security: dict[str, list[FilingDocument]] = {}
    for document in documents:
        by_security.setdefault(document.security_id, []).append(document)

    joined: list[Announcement] = []
    unjoined: list[LabelledEvent] = []

    for label in labels:
        target = label.event.announcement_date
        candidates = [
            document
            for document in by_security.get(label.security_id, [])
            if abs((document.filed - target).days) <= window_days
        ]
        if not candidates:
            unjoined.append(label)
            continue

        best = min(candidates, key=lambda d: (abs((d.filed - target).days), d.accession))
        joined.append(
            Announcement(
                announcement_id=f"{label.raw_event_id}::{best.accession}",
                security_id=label.security_id,
                text=best.text,
                provenance=Provenance(
                    source="sec-edgar",
                    url=best.url,
                    retrieved=retrieved,
                    sha256=hashlib.sha256(best.text.encode("utf-8")).hexdigest(),
                ),
                label=label.event,
                label_source=LabelSource.AUTO,
            )
        )

    return joined, unjoined
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_triage.py -v`
Expected: PASS.

- [ ] **Step 5: Add the SEC fetcher**

Append to `src/miniftse/triage/text.py`. Network-touching, so it is a separate function
with no test that hits the wire — the join above is where the logic lives and it is
tested purely.

```python
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
"""Filing index per issuer. The SEC requires a declared contact in the User-Agent and
rate-limits to 10 requests/second; ignoring either gets the IP blocked."""


def fetch_filings(
    cik: int,
    security_id: str,
    contact: str,
    *,
    forms: tuple[str, ...] = ("8-K",),
    since: dt.date | None = None,
    limit: int = 50,
) -> list[FilingDocument]:
    """Recent filings for one issuer. Requires network.

    Kept deliberately thin: it turns SEC JSON into `FilingDocument`, and nothing else.
    Every judgement about which filing matches which event lives in
    `join_labels_to_text`, which is pure and tested.
    """
    import requests

    headers = {"User-Agent": f"miniftse-research/0.1 (contact: {contact})"}
    response = requests.get(SUBMISSIONS_URL.format(cik=cik), headers=headers, timeout=30)
    response.raise_for_status()
    recent = response.json()["filings"]["recent"]

    documents: list[FilingDocument] = []
    for form, accession, filing_date, primary in zip(
        recent["form"],
        recent["accessionNumber"],
        recent["filingDate"],
        recent["primaryDocument"],
        strict=False,
    ):
        if form not in forms:
            continue
        filed = dt.date.fromisoformat(filing_date)
        if since and filed < since:
            continue

        bare = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{bare}/{primary}"
        body = requests.get(url, headers=headers, timeout=30)
        body.raise_for_status()
        documents.append(
            FilingDocument(
                accession=accession,
                filed=filed,
                url=url,
                text=body.text,
                security_id=security_id,
            )
        )
        if len(documents) >= limit:
            break
    return documents
```

**The returned `text` is raw HTML.** Stripping it to readable prose is a real step and
belongs here — add it once you have seen what an actual 8-K body looks like, rather than
guessing at a tag structure now.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run mypy src/miniftse/triage
git add src/miniftse/triage/text.py tests/test_triage.py
git commit -m "triage: join vendor labels to SEC filing text, dropping what will not join"
```

- [ ] **Step 7: Record the decision**

Append to `DECISIONS.md`:

```markdown
## D-023: Unjoinable labels are dropped and counted, not matched approximately

Vendor corporate actions carry no announcement text; SEC filings carry no structured
event. The join is issuer plus a date window, and it is a heuristic. A wrong join puts
a correct label on the wrong text and corrupts every downstream metric invisibly, so
the join fails closed and the unjoined count is published as a property of the corpus.

**Rejected:** widening the window until everything joins. That trades a visible gap for
an invisible error rate, which is the wrong trade in an eval set.
```

---

### Task 6 (stretch): LLM extraction on the easy classes

Do this only if Tasks 1–5 are committed and the week has room. If it slips to stage 2,
nothing is lost — every earlier task stands alone.

**Files:**
- Create: `src/miniftse/triage/extract.py`
- Modify: `tests/test_triage.py` (append)

**Interfaces:**
- Consumes: `miniftse.agents.llm.{LlmClient, Message, OfflineLlm}`, `taxonomy.{TAXONOMY, build_event}`, `corpus.Announcement`
- Produces: `Extraction` (fields: `event`, `abstained`, `reason`, `raw`), `extract_event(client, announcement_text, security_id, *, event_id) -> Extraction`

- [ ] **Step 1: Write the failing test with a scripted client**

Append to `tests/test_triage.py`:

```python
from miniftse.agents.llm import LlmClient, LlmResponse, Message
from miniftse.triage.extract import Extraction, extract_event


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
        client = ScriptedLlm(_json.dumps({
            "event_type": "CASH_DIVIDEND",
            "ex_date": "2024-06-10",
            "announcement_date": "2024-05-20",
            "pay_date": "2024-06-24",
            "payload": {"amount": 2.0},
        }))

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
        client = ScriptedLlm(_json.dumps({
            "event_type": "CASH_DIVIDEND",
            "ex_date": "2024-06-10",
            "announcement_date": "2024-05-20",
            "pay_date": "2024-06-24",
            "payload": {},
        }))

        result = extract_event(client, "…", "S0", event_id="E1")

        assert result.abstained is True
        assert "amount" in result.reason
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_triage.py::TestExtraction -v`
Expected: FAIL — no module `miniftse.triage.extract`.

- [ ] **Step 3: Implement**

Create `src/miniftse/triage/extract.py`:

```python
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
    return (
        f"Security: {security_id}\n"
        f"Permitted event_type values: {types}\n\n"
        f"Announcement:\n{text}"
    )


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
        event = build_event(
            EventType(parsed["event_type"]), common, parsed.get("payload", {})
        )
    except (KeyError, ValueError, TaxonomyError) as exc:
        return Extraction(None, True, str(exc), raw)

    return Extraction(event, False, "", raw)
```

- [ ] **Step 4: Run, lint, typecheck, commit**

```bash
uv run pytest tests/test_triage.py -v
uv run ruff check src tests && uv run ruff format src tests && uv run mypy src/miniftse/triage
git add src/miniftse/triage/extract.py tests/test_triage.py
git commit -m "triage: LLM extraction that abstains rather than half-parsing"
```

- [ ] **Step 5: Smoke-test against a real model, once**

Needs `ANTHROPIC_API_KEY` and `uv add anthropic`. `AnthropicLlm` and `CachingLlm` already
exist at `src/miniftse/agents/llm.py:264` and `:317`; use the caching wrapper so a
re-run costs nothing.

Run it over five joined announcements from Task 5, print each `Extraction`, and grade
with `impact_error` from Task 2. Do not turn this into a committed test — CI stays
offline and keyless. Record what you saw in the commit message.

---

## Definition of Done for Stage 1

- [ ] `uv run pytest tests/test_triage.py -v` passes
- [ ] `uv run ruff check src tests` and `ruff format --check` clean
- [ ] `uv run mypy src/miniftse/triage` clean
- [ ] `TAXONOMY` covers all 16 `EventType` values, unmapped ones annotated
- [ ] The 67.114 bps fixture passes with a hand-derivable working
- [ ] A corpus JSONL file exists with real joined announcements, and the unjoined count is recorded
- [ ] `DECISIONS.md` has D-022 and D-023
- [ ] Every task committed separately
