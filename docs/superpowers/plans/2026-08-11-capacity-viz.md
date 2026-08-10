# Capacity Visualization App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a static, GitHub-Pages-hosted visualization app (`viz/`) that lets anyone
see the index and interactively explore the factor-exposure/turnover/capacity trade-off,
without cloning the repo.

**Architecture:** A small backend addition threads ADV (average daily traded value)
through the existing `Constituent`/`ConstituentSpec`/`IndexStateFile` chain (it's
computed today but never persisted). A Python script, `viz/export.py`, turns existing
artefacts (plus that one new field) into four static JSON files. A plain HTML/CSS/JS
single-page app renders four tabs from those JSON files, with the capacity-trim
algorithm ported to JS so a fund-size slider recomputes live in the browser. No backend,
no build step, deployed via GitHub Actions to GitHub Pages.

**Tech Stack:** Python (existing `miniftse` package, `uv`, `pytest`), vanilla HTML/CSS/JS
with native ES modules (no npm, no bundler), Chart.js via CDN, Node.js (dev-time only,
for testing `capacity.js`).

## Global Constraints

- No backend, no authentication, no live recomputation of the index itself (spec:
  Non-goals).
- No npm, no frontend build step — plain HTML/CSS/JS with native ES modules
  (spec: Frontend tech decision).
- Chart.js loaded from a CDN `<script>` tag, pinned to a specific version
  (spec: Frontend section; version resolved below: `4.4.4`).
- `export.py` fails fast (non-zero exit) on any missing artefact rather than writing
  partial JSON (spec: Error handling).
- Constituents/capacity data is sourced from `artefacts/state/{index_id}_state.json`
  (the `daily`/`seed-state` snapshot), not from `build-index`'s history — that file is
  the "as of now" snapshot; `weights.parquet`/`levels.parquet` supply the full-history
  Overview chart (spec: Data pipeline).
- Index id is `MFTSE-GLOBAL` (the default in `IndexConfig`, `src/miniftse/config.py:193`).
- No new JS test suite beyond `capacity.js` (pure logic, testable with plain Node);
  other frontend tasks are verified manually in a browser (spec: Testing).

---

## Task 1: `Constituent`/`ConstituentSpec` gain an `adv` field; roll-forward preserves it

**Files:**
- Modify: `src/miniftse/calc/state.py:26-40` (`Constituent`)
- Modify: `src/miniftse/calc/index.py:45-61` (`ConstituentSpec`)
- Modify: `src/miniftse/calc/index.py:249-284` (`_to_constituent`, `_mark`)
- Test: `tests/test_index_maths.py`

**Interfaces:**
- Produces: `Constituent.adv: float = 0.0` (base currency, average daily traded value).
  `ConstituentSpec.adv: float = 0.0`. Later tasks (2, 3) populate this with real values;
  this task only adds the field and makes sure it survives construction and roll-forward
  instead of silently resetting to the default.

- [ ] **Step 1: Write the failing test — `_to_constituent` and `_mark` both carry `adv` through**

Add to `tests/test_index_maths.py` (near the other `IndexCalculator`/`Constituent`
tests — check the existing imports at the top of the file and reuse them):

```python
def test_to_constituent_carries_adv():
    from miniftse.calc.index import ConstituentSpec, IndexCalculator
    from miniftse.calc.fx import FxTable
    from miniftse.corpactions.engine import CorporateActionEngine
    import datetime as dt

    calc = IndexCalculator(
        config=global_all_cap(), fx=FxTable.identity(),
        engine=CorporateActionEngine(withholding_tax={}),
    )
    spec = ConstituentSpec(
        security_id="S1", shares=1000.0, free_float_factor=1.0, adv=5_000_000.0,
    )
    c = calc._to_constituent(spec, price=10.0, date=dt.date(2020, 1, 1))
    assert c.adv == 5_000_000.0


def test_mark_preserves_adv():
    from miniftse.calc.index import IndexCalculator, _PriceBook
    from miniftse.calc.state import Constituent, IndexState
    from miniftse.calc.fx import FxTable
    from miniftse.corpactions.engine import CorporateActionEngine
    import datetime as dt
    import pandas as pd

    calc = IndexCalculator(
        config=global_all_cap(), fx=FxTable.identity(),
        engine=CorporateActionEngine(withholding_tax={}),
    )
    state = IndexState(
        date=dt.date(2020, 1, 1), divisor=1.0,
        constituents={"S1": Constituent("S1", price=10.0, shares=1000.0, adv=5_000_000.0)},
    )
    book = _PriceBook(pd.DataFrame({
        "security_id": ["S1"], "date": [dt.date(2020, 1, 2)], "close": [11.0],
    }))
    rolled = calc._mark(state, dt.date(2020, 1, 2), book)
    assert rolled.constituents["S1"].adv == 5_000_000.0
    assert rolled.constituents["S1"].price == 11.0
```

Check `FxTable` for an `identity()` constructor or equivalent zero-config instance
already used elsewhere in `tests/test_index_maths.py` before writing this — reuse
whatever the existing tests in that file already use to build an `IndexCalculator`
rather than inventing a new pattern. Adjust the two `IndexCalculator(...)` constructions
above to match.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_index_maths.py -k "carries_adv or preserves_adv" -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'adv'`
(neither `Constituent` nor `ConstituentSpec` has the field yet).

- [ ] **Step 3: Add the field and thread it through**

In `src/miniftse/calc/state.py`, add to `Constituent` (after `icb_industry`, before
`size_band` is fine — order among defaulted fields doesn't matter since all call sites
use keyword arguments):

```python
    adv: float = 0.0
    """Average daily traded value, in index base currency. Drives the capacity
    constraint (`weighting.schemes.capacity_constrained_weights`); not otherwise used
    by the calculation engine."""
```

In `src/miniftse/calc/index.py`, add the same field to `ConstituentSpec`:

```python
    icb_industry: str = ""
    adv: float = 0.0
    """Average daily traded value, in index base currency. See `Constituent.adv`."""
```

Update `_to_constituent` (`calc/index.py:249-263`) to pass it through:

```python
    def _to_constituent(
        self, spec: ConstituentSpec, price: float, date: dt.date
    ) -> Constituent:
        return Constituent(
            security_id=spec.security_id,
            price=price,
            shares=spec.shares,
            free_float_factor=spec.free_float_factor,
            capping_factor=spec.capping_factor,
            fx_rate=self.fx.rate(date, str(spec.currency)),
            currency=spec.currency,
            country=spec.country,
            icb_industry=spec.icb_industry,
            size_band=spec.size_band,
            adv=spec.adv,
        )
```

Update `_mark` (`calc/index.py:265-284`) — this one matters: it rebuilds `Constituent`
field-by-field rather than using `replace()`, so a missed field silently resets to the
default on every single day the index rolls forward:

```python
            updated[sec_id] = Constituent(
                security_id=c.security_id, price=price, shares=c.shares,
                free_float_factor=c.free_float_factor, capping_factor=c.capping_factor,
                fx_rate=self.fx.rate(date, str(c.currency)), currency=c.currency,
                country=c.country, icb_industry=c.icb_industry, size_band=c.size_band,
                is_suspended=price_book.is_suspended(sec_id, date),
                adv=c.adv,
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_index_maths.py -k "carries_adv or preserves_adv" -v`
Expected: PASS

- [ ] **Step 5: Run the full fast test suite to check nothing else broke**

Run: `uv run pytest tests/test_index_maths.py tests/test_properties.py -q`
Expected: all pass — adding a defaulted field should not change any existing behaviour.

- [ ] **Step 6: Commit**

```bash
git add src/miniftse/calc/state.py src/miniftse/calc/index.py tests/test_index_maths.py
git commit -m "calc: add adv field to Constituent/ConstituentSpec, thread through roll-forward"
```

---

## Task 2: Spin-off children inherit the parent's `adv`

**Files:**
- Modify: `src/miniftse/corpactions/engine.py:281-292`
- Test: `tests/test_index_maths.py`

**Interfaces:**
- Consumes: `Constituent.adv` (Task 1).
- Produces: nothing new consumed elsewhere — this closes the last place a `Constituent`
  is built field-by-field instead of via `Constituent.with_price()`/`replace()`.

**Rationale (for whoever reviews this task):** every other capping/currency/country
field is explicitly inherited from the parent here already, with a comment explaining
why (continuity of total market value). ADV is genuinely uncertain for a brand-new
spinco, but leaving it at the dataclass default of `0.0` is worse: `0.0` reads to the
capacity view as "zero liquidity, cap this name to nothing", which is a false, specific
claim — parent's ADV is a defensible placeholder until the next review recomputes it
for real, consistent with how `capping_factor` is already handled two lines above.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_index_maths.py` (find the existing spin-off test — search the file
for `Spinoff` or `_apply_spinoff` and place this next to it, reusing whatever fixture
setup that test already uses for the engine/state):

```python
def test_spinoff_child_inherits_parent_adv():
    from miniftse.corpactions.engine import CorporateActionEngine
    from miniftse.corpactions.events import Spinoff
    from miniftse.calc.state import Constituent, IndexState
    import datetime as dt

    engine = CorporateActionEngine(withholding_tax={})
    parent = Constituent("PARENT", price=100.0, shares=1000.0, adv=8_000_000.0)
    state = IndexState(date=dt.date(2020, 1, 1), divisor=1.0,
                        constituents={"PARENT": parent})
    event = Spinoff(
        event_id="SPIN-1", security_id="PARENT",
        ex_date=dt.date(2020, 1, 1), announcement_date=dt.date(2019, 12, 1),
        pay_date=dt.date(2020, 1, 1),
        spinco_security_id="CHILD",
        shares_per_parent_share=1.0,
        value_per_parent_share=10.0,
        parent_cum_price=100.0,
        spinco_enters_index=True,
    )
    handled = engine._apply_spinoff(event, state)
    assert handled.state.constituents["CHILD"].adv == 8_000_000.0
```

`Spinoff`'s exact fields (`src/miniftse/corpactions/events.py:402-412`): `event_id,
security_id, ex_date, announcement_date, pay_date, spinco_security_id,
shares_per_parent_share, value_per_parent_share, parent_cum_price,
spinco_enters_index=True, currency="USD"`. `spinco_price` is a computed `@property`
(`value_per_parent_share / shares_per_parent_share`), not a constructor argument —
don't pass it. `_Handled.state` (`engine.py:49`) is the field holding the resulting
`IndexState`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_index_maths.py -k spinoff_child_inherits_parent_adv -v`
Expected: FAIL — `assert 0.0 == 8_000_000.0`

- [ ] **Step 3: Pass `adv=c.adv` in the spinco construction**

In `src/miniftse/corpactions/engine.py`, in `_apply_spinoff` (around line 281-292):

```python
            spinco = Constituent(
                security_id=event.spinco_security_id,
                price=event.spinco_price,
                shares=c.shares * event.shares_per_parent_share,
                free_float_factor=c.free_float_factor,
                capping_factor=c.capping_factor,
                fx_rate=c.fx_rate,
                currency=c.currency,
                country=c.country,
                icb_industry=c.icb_industry,
                size_band=c.size_band,
                adv=c.adv,
            )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_index_maths.py -k spinoff_child_inherits_parent_adv -v`
Expected: PASS

- [ ] **Step 5: Run the full fast suite**

Run: `uv run pytest tests/test_index_maths.py tests/test_properties.py -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/miniftse/corpactions/engine.py tests/test_index_maths.py
git commit -m "corpactions: spin-off children inherit the parent's adv"
```

---

## Task 3: Reviews populate `ConstituentSpec.adv` with a real value

**Files:**
- Modify: `src/miniftse/review/reconstitution.py:299-311`
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: `ConstituentSpec.adv` (Task 1), `SecurityMetrics.median_daily_traded_value`
  (already exists: `src/miniftse/universe/screens.py:87`).
- Produces: constituents coming out of a review now carry a real, non-zero `adv`
  (previously computed into `SecurityInputs.adv` for weighting purposes only, at
  `reconstitution.py:241`, and then discarded).

- [ ] **Step 1: Write the failing test**

This needs a full review to run, so it belongs in the integration suite. The engine
that implements `UniverseSource` is `ReconstitutionEngine`
(`src/miniftse/review/reconstitution.py:122-160`); the real production code
constructs it from a `SyntheticUniverse`'s raw generated frames, not from the
`SyntheticUniverse` itself — mirror `src/miniftse/production/build.py:95-124`'s
construction exactly (that's the authoritative example: it builds `prices`, `shares`,
`securities` and an FX spot-rate dict the same way this test needs to). Add to
`tests/test_integration.py` — check the top of the file for an existing
`SyntheticUniverse`/`SyntheticConfig` import and fixture helper (there is one, around
line 33, `n_securities=60, seed=20260809`) and reuse it rather than picking new
constants:

```python
def test_review_populates_constituent_adv(self):
    from miniftse.calc.fx import FxTable
    from miniftse.config import global_all_cap
    from miniftse.review.reconstitution import ReconstitutionEngine

    universe = SyntheticUniverse(SyntheticConfig(n_securities=60, seed=20260809))
    prices = universe._generated["prices"]
    shares = universe._generated["shares"]
    securities = universe.get_securities()
    config = global_all_cap()

    quotes = list(universe._fx["quote"].unique())
    fx = FxTable.from_frame(
        universe.get_fx("USD", quotes, universe.config.start, universe.config.end),
        universe.get_deposit_rates(quotes, universe.config.start, universe.config.end),
        base=str(config.base_currency),
    )
    spot = {c: fx.rate(config.base_date, c) for c in fx.currencies()}

    reconstitution = ReconstitutionEngine(
        config=config, prices=prices, shares=shares, securities=securities,
        fx_rates=spot,
    )
    # No effective_dates() call first -> constituents_for falls back to screening at
    # the date itself (reconstitution.py:186-189), which is exactly what's needed
    # here: run one review, check what it produced.
    specs = reconstitution.constituents_for(config.base_date)
    assert specs, "review produced no constituents - check the fixture"
    assert any(spec.adv > 0.0 for spec in specs.values())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_integration.py -k review_populates_constituent_adv -v`
Expected: FAIL — every `spec.adv` is `0.0` (the dataclass default; nothing sets it yet).

- [ ] **Step 3: Populate `adv` at construction time**

In `src/miniftse/review/reconstitution.py`, in `run_review`, the `ConstituentSpec`
construction loop (around line 299-311) already has `meta = self._meta.get(sid, {})`
and iterates `for sid in in_scope:` where `in_scope[sid]` is the `SecurityMetrics`
object also used two blocks earlier (line 241) to build `SecurityInputs.adv`. Add one
line:

```python
        for sid in in_scope:
            if sid not in share_lookup.index:
                continue
            sh = share_lookup.loc[sid]
            meta = self._meta.get(sid, {})
            metrics = in_scope[sid]
            factor = (
                capped.weights[sid] / float_cap[sid]
                if float_cap.get(sid) else 1.0
            )
            constituents[sid] = ConstituentSpec(
                security_id=sid,
                shares=float(sh["shares_outstanding"]),
                free_float_factor=min(
                    float(sh["free_float_factor"]),
                    float(sh.get("foreign_ownership_limit", 1.0)),
                ),
                capping_factor=factor,
                size_band=bands[sid].band if sid in bands else SizeBand.LARGE,
                currency=meta.get("currency", Currency.USD),  # type: ignore[arg-type]
                country=meta.get("country", Country.US),  # type: ignore[arg-type]
                icb_industry=str(meta.get("icb_industry", "")),
                adv=metrics.median_daily_traded_value,
            )
```

(Only the `metrics = in_scope[sid]` line and the trailing `adv=...` argument are new;
everything else shown is the existing body, included so the insertion point is
unambiguous.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_integration.py -k review_populates_constituent_adv -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/miniftse/review/reconstitution.py tests/test_integration.py
git commit -m "review: populate ConstituentSpec.adv from review-time liquidity metrics"
```

---

## Task 4: Daily production persists `adv` through `IndexStateFile`

**Files:**
- Modify: `src/miniftse/production/daily.py:66-97` (`IndexStateFile.from_state`, `.to_state`)
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: `Constituent.adv` (Task 1).
- Produces: `artefacts/state/{index_id}_state.json`'s per-constituent objects now have
  an `"adv"` key. This is the field `viz/export.py` (Task 6) reads. Old state files
  saved before this change have no `"adv"` key — `to_state()` must tolerate that (used
  by anyone resuming from a state file written before this task).

- [ ] **Step 1: Write the failing test — round-trip preserves adv, and old files without it still load**

Add to `tests/test_integration.py` (find the existing `IndexStateFile` round-trip test
— search for `IndexStateFile` — and place these next to it):

```python
def test_state_file_round_trips_adv(self, tmp_path):
    state = IndexState(
        date=dt.date(2020, 1, 1), divisor=1.0,
        constituents={"S1": Constituent("S1", price=10.0, shares=100.0, adv=1_234_567.0)},
    )
    saved = IndexStateFile.from_state("MFTSE-TEST", state, pr=100.0, gtr=100.0, ntr=100.0)
    saved.save(tmp_path)
    loaded = IndexStateFile.load(tmp_path, "MFTSE-TEST")
    restored = loaded.to_state()
    assert restored.constituents["S1"].adv == 1_234_567.0

def test_state_file_without_adv_key_loads_with_zero_default(self, tmp_path):
    # Simulates a state file written before this field existed.
    import json
    path = tmp_path / "MFTSE-OLD_state.json"
    path.write_text(json.dumps({
        "index_id": "MFTSE-OLD", "as_of": "2020-01-01", "divisor": 1.0,
        "level_pr": 100.0, "level_gtr": 100.0, "level_ntr": 100.0,
        "constituents": {
            "S1": {
                "price": 10.0, "shares": 100.0, "free_float_factor": 1.0,
                "capping_factor": 1.0, "fx_rate": 1.0, "currency": "USD",
                "country": "US", "icb_industry": "", "size_band": "LARGE",
            }
        },
    }), encoding="utf-8")
    loaded = IndexStateFile.load(tmp_path, "MFTSE-OLD")
    restored = loaded.to_state()
    assert restored.constituents["S1"].adv == 0.0
```

Check the top of `tests/test_integration.py` for how `IndexStateFile`, `IndexState`,
`Constituent`, and `dt` are already imported and reuse those imports rather than
re-importing. If the existing `IndexStateFile` round-trip test in that file uses a
different fixture pattern (e.g. a shared `tmp_path`-based helper), match its style.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_integration.py -k "round_trips_adv or without_adv_key" -v`
Expected: both FAIL — `from_state` doesn't write `"adv"`, and the second test's
`KeyError: 'adv'` inside `to_state()` (it currently does `v["adv"]` unconditionally
once the field exists on `Constituent` — actually today it doesn't reference `adv` at
all, so this test will fail only once Step 3 is half-done if `to_state` uses `v["adv"]`
without a default; write `to_state` with `.get()` from the start in Step 3 so this
test is meaningful).

- [ ] **Step 3: Persist `adv`, tolerating its absence on load**

In `src/miniftse/production/daily.py`, update `IndexStateFile.from_state`:

```python
            constituents={
                k: {
                    "price": c.price, "shares": c.shares,
                    "free_float_factor": c.free_float_factor,
                    "capping_factor": c.capping_factor, "fx_rate": c.fx_rate,
                    "currency": str(c.currency), "country": str(c.country),
                    "icb_industry": c.icb_industry, "size_band": str(c.size_band),
                    "adv": c.adv,
                }
                for k, c in state.constituents.items()
            },
```

And `IndexStateFile.to_state`:

```python
            constituents={
                k: Constituent(
                    security_id=k, price=v["price"], shares=v["shares"],
                    free_float_factor=v["free_float_factor"],
                    capping_factor=v["capping_factor"], fx_rate=v["fx_rate"],
                    currency=Currency(v["currency"]), country=Country(v["country"]),
                    icb_industry=v["icb_industry"], size_band=SizeBand(v["size_band"]),
                    adv=v.get("adv", 0.0),
                )
                for k, v in self.constituents.items()
            },
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_integration.py -k "round_trips_adv or without_adv_key" -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/miniftse/production/daily.py tests/test_integration.py
git commit -m "production: persist adv through the daily state file"
```

---

## Task 5: `viz/export.py` — `overview.json`

**Files:**
- Create: `viz/export.py`
- Test: `tests/test_viz_export.py`

**Interfaces:**
- Produces: `build_overview(levels: pd.DataFrame) -> dict` with shape:
  ```
  {
    "index_id": str,
    "dates": list[str],       # ISO date strings, ascending
    "pr": list[float], "gtr": list[float], "ntr": list[float],
    "stats": {
      "annualised_return": float, "annualised_vol": float,
      "max_drawdown": float, "divisor_events": int,
    },
  }
  ```
  `levels` must have columns `date, price_return, gross_total_return,
  net_total_return, divisor` (the shape of `MFTSE-GLOBAL_levels.parquet`, confirmed:
  `date, price_return, gross_total_return, net_total_return, divisor, n_constituents,
  total_market_value, dividend_points, net_dividend_points`).
- Consumed by: Task 8 (`main()`), Task 10 (`render_overview.js`, via the JSON file).

- [ ] **Step 1: Write the failing test**

Create `tests/test_viz_export.py`:

```python
"""Tests for viz/export.py — a standalone script outside src/, loaded by path."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

EXPORT_PATH = Path(__file__).resolve().parents[1] / "viz" / "export.py"
_spec = importlib.util.spec_from_file_location("viz_export", EXPORT_PATH)
export = importlib.util.module_from_spec(_spec)
sys.modules["viz_export"] = export
_spec.loader.exec_module(export)


def _levels_fixture() -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2020-01-01", "2020-01-02", "2020-01-03"],
        "price_return": [1000.0, 1100.0, 990.0],
        "gross_total_return": [1000.0, 1100.0, 990.0],
        "net_total_return": [1000.0, 1095.0, 986.0],
        "divisor": [100.0, 100.0, 105.0],
    })


class TestBuildOverview:
    def test_series_pass_through(self):
        result = export.build_overview(_levels_fixture())
        assert result["dates"] == ["2020-01-01", "2020-01-02", "2020-01-03"]
        assert result["gtr"] == [1000.0, 1100.0, 990.0]

    def test_max_drawdown(self):
        result = export.build_overview(_levels_fixture())
        # peak 1100 on day 2, trough 990 on day 3 -> 990/1100 - 1 = -0.1 exactly
        assert result["stats"]["max_drawdown"] == pytest.approx(-0.1)

    def test_divisor_events_counts_changes_only(self):
        result = export.build_overview(_levels_fixture())
        # divisor is unchanged day1->day2, changes 100->105 day2->day3: exactly 1 event
        assert result["stats"]["divisor_events"] == 1

    def test_annualised_return_is_cagr_over_trading_days(self):
        result = export.build_overview(_levels_fixture())
        expected = (990.0 / 1000.0) ** (252 / 2) - 1
        assert result["stats"]["annualised_return"] == pytest.approx(expected)

    def test_annualised_vol_matches_stdev_of_daily_returns(self):
        result = export.build_overview(_levels_fixture())
        # daily returns: 1100/1000-1=0.10, 990/1100-1=-0.1 (both exact); sample stdev
        # of [0.10, -0.10] is sqrt(0.02) = 0.1414213562373095
        expected = (0.02 ** 0.5) * (252 ** 0.5)
        assert result["stats"]["annualised_vol"] == pytest.approx(expected)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_viz_export.py -v`
Expected: FAIL — `viz/export.py` does not exist yet (`FileNotFoundError` from
`spec_from_file_location`/`exec_module`, or a collection error).

- [ ] **Step 3: Create `viz/export.py` with `build_overview`**

```python
"""Generates viz/data/*.json from artefacts a real miniftse build/daily run produces.

Run from the repo root: `uv run python viz/export.py` (or `make viz`). Reads from
artefacts/, never writes there - only to viz/data/. Fails fast (non-zero exit) if an
expected artefact is missing, rather than writing partial JSON.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTEFACTS_DIR = REPO_ROOT / "artefacts"
DATA_DIR = Path(__file__).resolve().parent / "data"
INDEX_ID = "MFTSE-GLOBAL"

TRADING_DAYS_PER_YEAR = 252


class MissingArtefactError(RuntimeError):
    """An expected artefact file is missing."""


def build_overview(levels: pd.DataFrame) -> dict:
    """levels: date, price_return, gross_total_return, net_total_return, divisor -
    the shape of {index_id}_levels.parquet."""
    df = levels.sort_values("date").reset_index(drop=True)
    gtr = df["gross_total_return"]
    daily_ret = gtr.pct_change().dropna()
    n_days = len(df) - 1

    annualised_return = (
        (gtr.iloc[-1] / gtr.iloc[0]) ** (TRADING_DAYS_PER_YEAR / n_days) - 1
        if n_days > 0 and gtr.iloc[0] > 0 else 0.0
    )
    annualised_vol = (
        float(daily_ret.std(ddof=1)) * (TRADING_DAYS_PER_YEAR ** 0.5)
        if len(daily_ret) > 1 else 0.0
    )
    drawdown = gtr / gtr.cummax() - 1.0
    max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
    divisor_events = int((df["divisor"].diff().abs() > 1e-9).sum())

    return {
        "index_id": INDEX_ID,
        "dates": df["date"].astype(str).tolist(),
        "pr": df["price_return"].tolist(),
        "gtr": df["gross_total_return"].tolist(),
        "ntr": df["net_total_return"].tolist(),
        "stats": {
            "annualised_return": float(annualised_return),
            "annualised_vol": float(annualised_vol),
            "max_drawdown": max_drawdown,
            "divisor_events": divisor_events,
        },
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_viz_export.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add viz/export.py tests/test_viz_export.py
git commit -m "viz: export.py builds overview.json from levels.parquet"
```

---

## Task 6: `viz/export.py` — `constituents.json` and `capacity.json`

**Files:**
- Modify: `viz/export.py`
- Test: `tests/test_viz_export.py`

**Interfaces:**
- Consumes: `SCHEME_PROPERTIES` (`src/miniftse/weighting/schemes.py:258-301`, already
  exists, no change needed).
- Produces:
  - `build_constituents(weights: pd.DataFrame, state: dict) -> list[dict]` — each
    entry: `{security_id: str, weight: float, adv: float, sector: str, country: str,
    size_band: str}`, sorted by `weight` descending. `weights` has columns
    `date, security_id, weight`. `state` is the `"constituents"` sub-dict of a parsed
    `{index_id}_state.json` (keys are security ids, values have `adv, icb_industry,
    country, size_band`, per Task 4).
  - `build_capacity(constituents: list[dict]) -> dict` — `{"schemes":
    SCHEME_PROPERTIES, "constituents": constituents, "capacity_params": {"participation":
    0.20, "max_days_to_trade": 5.0}}`.
- Consumed by: Task 8 (`main()`), Task 12 (`render_constituents.js`),
  Task 11 (`render_capacity.js`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_viz_export.py`:

```python
def _weights_fixture() -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2020-01-01", "2020-01-01", "2020-01-02", "2020-01-02"],
        "security_id": ["S1", "S2", "S1", "S2"],
        "weight": [0.10, 0.20, 0.60, 0.30],  # latest date (01-02) should win
    })


def _state_fixture() -> dict:
    return {
        "S1": {"adv": 1_000_000.0, "icb_industry": "45", "country": "US",
               "size_band": "LARGE"},
        "S2": {"adv": 2_000_000.0, "icb_industry": "15", "country": "GB",
               "size_band": "MID"},
    }


class TestBuildConstituents:
    def test_uses_latest_date_only(self):
        result = export.build_constituents(_weights_fixture(), _state_fixture())
        assert {r["security_id"]: r["weight"] for r in result} == {"S1": 0.60, "S2": 0.30}

    def test_sorted_by_weight_descending(self):
        result = export.build_constituents(_weights_fixture(), _state_fixture())
        assert [r["security_id"] for r in result] == ["S1", "S2"]

    def test_joins_state_metadata(self):
        result = export.build_constituents(_weights_fixture(), _state_fixture())
        s1 = next(r for r in result if r["security_id"] == "S1")
        assert s1 == {
            "security_id": "S1", "weight": 0.60, "adv": 1_000_000.0,
            "sector": "45", "country": "US", "size_band": "LARGE",
        }

    def test_missing_state_entry_defaults_gracefully(self):
        weights = pd.DataFrame({
            "date": ["2020-01-01"], "security_id": ["S9"], "weight": [1.0],
        })
        result = export.build_constituents(weights, {})
        assert result == [{
            "security_id": "S9", "weight": 1.0, "adv": 0.0,
            "sector": "", "country": "", "size_band": "",
        }]


class TestBuildCapacity:
    def test_shape(self):
        constituents = export.build_constituents(_weights_fixture(), _state_fixture())
        result = export.build_capacity(constituents)
        assert result["constituents"] == constituents
        assert result["capacity_params"] == {
            "participation": 0.20, "max_days_to_trade": 5.0,
        }
        assert "float_market_cap" in result["schemes"]
        assert "capacity" in result["schemes"]["float_market_cap"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_viz_export.py -k "BuildConstituents or BuildCapacity" -v`
Expected: FAIL — `AttributeError: module 'viz_export' has no attribute
'build_constituents'`

- [ ] **Step 3: Add the two functions to `viz/export.py`**

```python
def build_constituents(weights: pd.DataFrame, state: dict) -> list[dict]:
    """weights: date, security_id, weight (the shape of {index_id}_weights.parquet).
    state: the "constituents" sub-dict of a parsed {index_id}_state.json - keyed by
    security_id, values carrying adv/icb_industry/country/size_band (see
    production.daily.IndexStateFile, after Task 4)."""
    latest_date = weights["date"].max()
    latest = weights[weights["date"] == latest_date]

    rows = []
    for _, row in latest.iterrows():
        sid = row["security_id"]
        meta = state.get(sid, {})
        rows.append({
            "security_id": sid,
            "weight": float(row["weight"]),
            "adv": float(meta.get("adv", 0.0)),
            "sector": meta.get("icb_industry", ""),
            "country": meta.get("country", ""),
            "size_band": meta.get("size_band", ""),
        })
    rows.sort(key=lambda r: r["weight"], reverse=True)
    return rows


def build_capacity(constituents: list[dict]) -> dict:
    from miniftse.weighting.schemes import SCHEME_PROPERTIES

    return {
        "schemes": SCHEME_PROPERTIES,
        "constituents": constituents,
        "capacity_params": {"participation": 0.20, "max_days_to_trade": 5.0},
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_viz_export.py -k "BuildConstituents or BuildCapacity" -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full viz test file**

Run: `uv run pytest tests/test_viz_export.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add viz/export.py tests/test_viz_export.py
git commit -m "viz: export.py builds constituents.json and capacity.json"
```

---

## Task 7: `viz/export.py` — `risk_attribution.json` (one-pager markdown parser)

**Files:**
- Modify: `viz/export.py`
- Test: `tests/test_viz_export.py`

**Interfaces:**
- Produces: `parse_onepager(text: str) -> dict` with shape:
  ```
  {
    "title": str,          # from the "# ..." line
    "meta": str,           # the bold metadata line under the title, ** stripped
    "sections": [
      {"heading": str, "text": str, "table": {"columns": [...], "rows": [[...]]} | None},
      ...
    ],
  }
  ```
- Consumed by: Task 8 (`main()`), Task 14 (`render_risk.js`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_viz_export.py`. The fixture mirrors the real structure of
`artefacts/risk_onepager.md` (title, bold metadata line, `## Headline` prose-only
section, then a section with a table):

```python
ONEPAGER_FIXTURE = """\
# Risk report — miniFTSE Global Value Tilt

**Against** miniFTSE Global All Cap · **As at** 2021-01-14

---

## Headline

Forecast tracking error: **10.90% a year.**

Tracking error is how far this index is expected to move away from its parent.

## Where the risk comes from

| Source | Exposure | Share of risk |
|---|---:|---:|
| Value | +1.202 | 45.3% |
| Quality | +0.594 | 17.8% |
"""


class TestParseOnepager:
    def test_title_and_meta(self):
        result = export.parse_onepager(ONEPAGER_FIXTURE)
        assert result["title"] == "Risk report — miniFTSE Global Value Tilt"
        assert result["meta"] == "Against miniFTSE Global All Cap · As at 2021-01-14"

    def test_prose_only_section_has_no_table(self):
        result = export.parse_onepager(ONEPAGER_FIXTURE)
        headline = next(s for s in result["sections"] if s["heading"] == "Headline")
        assert headline["table"] is None
        assert "10.90% a year." in headline["text"]

    def test_table_section_parsed(self):
        result = export.parse_onepager(ONEPAGER_FIXTURE)
        risk_section = next(
            s for s in result["sections"] if s["heading"] == "Where the risk comes from"
        )
        assert risk_section["table"]["columns"] == ["Source", "Exposure", "Share of risk"]
        assert risk_section["table"]["rows"] == [
            ["Value", "+1.202", "45.3%"],
            ["Quality", "+0.594", "17.8%"],
        ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_viz_export.py -k ParseOnepager -v`
Expected: FAIL — `AttributeError: module 'viz_export' has no attribute 'parse_onepager'`

- [ ] **Step 3: Add `parse_onepager`**

```python
import re


def parse_onepager(text: str) -> dict:
    """Parse a generated one-pager (risk_onepager.md / attribution_onepager.md) into
    JSON, without re-running the analysis that produced it. Each file is: a title
    line, a bold metadata line, then a series of "## Heading" sections holding prose
    and/or a markdown table."""
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("# ") else ""

    meta = ""
    for line in lines[1:6]:
        if line.strip().startswith("**"):
            meta = line.strip().replace("**", "")
            break

    section_parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)[1:]
    sections = []
    for i in range(0, len(section_parts), 2):
        heading = section_parts[i].strip()
        body_lines = section_parts[i + 1].splitlines()

        table_lines = [
            ln for ln in body_lines
            if ln.strip().startswith("|") and ln.strip().endswith("|")
        ]
        table = None
        if len(table_lines) >= 2:
            columns = [c.strip() for c in table_lines[0].strip("|").split("|")]
            rows = [
                [c.strip() for c in ln.strip("|").split("|")]
                for ln in table_lines[2:]  # skip header row + "---" separator row
            ]
            table = {"columns": columns, "rows": rows}

        prose_lines = [
            ln for ln in body_lines
            if ln.strip() and not ln.strip().startswith("|")
            and not ln.strip().startswith("---")
        ]
        text_body = " ".join(prose_lines).replace("**", "")

        sections.append({"heading": heading, "text": text_body, "table": table})

    return {"title": title, "meta": meta, "sections": sections}
```

Add the `import re` to the existing import block at the top of `viz/export.py`
(alongside `from pathlib import Path` etc.), not as a second scattered import.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_viz_export.py -k ParseOnepager -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Sanity-check against the real files** (not an automated test — a quick
  manual check that the parser survives contact with the actual generated files, which
  have more sections and a second table than the fixture)

Run:
```bash
uv run python -c "
import sys; sys.path.insert(0, 'viz')
import export
doc = export.parse_onepager(open('artefacts/risk_onepager.md', encoding='utf-8').read())
print(doc['title'])
print(doc['meta'])
for s in doc['sections']:
    print('-', s['heading'], '(table)' if s['table'] else '(prose)')
"
```
Expected: prints a title, a meta line, and one line per `##` section in the real file
(Headline, Where the risk comes from, Largest individual contributors, Is the forecast
any good?) with the two data-table sections correctly marked `(table)`. If a section
prints wrong (e.g. the disclaimer/footer text at the end gets folded into the last
section as prose) that is expected and acceptable per the design spec — fix only if a
section that should have a table shows `(prose)` instead.

- [ ] **Step 6: Commit**

```bash
git add viz/export.py tests/test_viz_export.py
git commit -m "viz: export.py parses the risk/attribution one-pagers into JSON"
```

---

## Task 8: `viz/export.py` — `main()`, fail-fast, and `make viz`

**Files:**
- Modify: `viz/export.py`
- Modify: `Makefile:1-2` (`.PHONY` line), add a `viz` target
- Test: `tests/test_viz_export.py`

**Interfaces:**
- Produces: running `uv run python viz/export.py` writes
  `viz/data/{overview,capacity,constituents,risk_attribution}.json`. Raises
  `MissingArtefactError` (defined in Task 5) and exits 1 if any input artefact is
  absent.
- Consumed by: Task 9 (frontend fetches these four files by path).

- [ ] **Step 1: Write the failing test — fail-fast on a missing artefact**

Append to `tests/test_viz_export.py`:

```python
class TestMainFailsFast:
    def test_raises_on_missing_artefact(self, tmp_path, monkeypatch):
        monkeypatch.setattr(export, "ARTEFACTS_DIR", tmp_path / "does-not-exist")
        monkeypatch.setattr(export, "DATA_DIR", tmp_path / "data")
        with pytest.raises(export.MissingArtefactError):
            export.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_viz_export.py -k raises_on_missing_artefact -v`
Expected: FAIL — `AttributeError: module 'viz_export' has no attribute 'main'`

- [ ] **Step 3: Add `main()` and the `_require`/`_write` helpers**

```python
import json
import sys


def _require(path: Path) -> Path:
    if not path.exists():
        raise MissingArtefactError(
            f"missing artefact: {path}. Run `make build-index` and `make seed-state` "
            "(or `make daily`) first."
        )
    return path


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    levels = pd.read_parquet(_require(ARTEFACTS_DIR / f"{INDEX_ID}_levels.parquet"))
    weights = pd.read_parquet(_require(ARTEFACTS_DIR / f"{INDEX_ID}_weights.parquet"))
    state_path = _require(ARTEFACTS_DIR / "state" / f"{INDEX_ID}_state.json")
    state_full = json.loads(state_path.read_text(encoding="utf-8"))
    risk_text = _require(ARTEFACTS_DIR / "risk_onepager.md").read_text(encoding="utf-8")
    attrib_text = _require(
        ARTEFACTS_DIR / "attribution_onepager.md"
    ).read_text(encoding="utf-8")

    constituents = build_constituents(weights, state_full["constituents"])

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _write(DATA_DIR / "overview.json", build_overview(levels))
    _write(DATA_DIR / "capacity.json", build_capacity(constituents))
    _write(DATA_DIR / "constituents.json", {
        "as_of": state_full["as_of"], "constituents": constituents,
    })
    _write(DATA_DIR / "risk_attribution.json", {
        "risk": parse_onepager(risk_text),
        "attribution": parse_onepager(attrib_text),
    })
    print(f"wrote 4 files to {DATA_DIR}")


if __name__ == "__main__":
    try:
        main()
    except MissingArtefactError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        sys.exit(1)
```

Move the `import json` and `import sys` up into the existing top-of-file import block
alongside `import re`, `from pathlib import Path`, and `import pandas as pd` — don't
leave imports scattered mid-file; this listing shows them separately only to make the
diff against Tasks 5-7 clear.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_viz_export.py -k raises_on_missing_artefact -v`
Expected: PASS

- [ ] **Step 5: Run the full export test file**

Run: `uv run pytest tests/test_viz_export.py -v`
Expected: all pass (should be ~17 tests across Tasks 5-8)

- [ ] **Step 6: Run it for real against this repo's actual artefacts**

Prerequisite: `artefacts/state/MFTSE-GLOBAL_state.json` must exist and (after Tasks
1-4) carry real `adv` values. If it was seeded before this plan's backend changes
landed, re-seed it first: `uv run miniftse seed-state`. Then:

```bash
uv run python viz/export.py
```
Expected: `wrote 4 files to .../viz/data`, and `viz/data/*.json` exist. Spot-check
`viz/data/capacity.json` — confirm at least some `constituents[].adv` values are
non-zero (if they're all `0.0`, the state file predates Task 4 and needs re-seeding).

- [ ] **Step 7: Add the `make viz` target**

In `Makefile`, add `viz` to the `.PHONY` line (line 1-2) and add a new target near
`docs:` (line 62-64):

```makefile
viz:  ## Regenerate the visualization site's data from artefacts/
	$(UV) run python viz/export.py
```

- [ ] **Step 8: Verify the Makefile target**

Run: `make viz`
Expected: same output as Step 6.

- [ ] **Step 9: Commit**

```bash
git add viz/export.py viz/data Makefile tests/test_viz_export.py
git commit -m "viz: export.py main() orchestration, fail-fast, make viz target"
```

---

## Task 9: Frontend shell — `index.html`, `style.css`, `util.js`, `app.js`

**Files:**
- Create: `viz/index.html`
- Create: `viz/style.css`
- Create: `viz/util.js`
- Create: `viz/app.js`

**Interfaces:**
- Produces:
  - `util.js` exports `fetchJSON(path: string): Promise<any>` (throws on non-2xx) and
    `showError(panelId: string, err: Error): void`.
  - `app.js` wires four tab buttons (`data-tab="overview"|"capacity"|"constituents"|
    "risk"`) to four panels (`id="tab-overview"` etc.), dynamically `import()`-ing
    `./render_overview.js`, `./render_capacity.js`, `./render_constituents.js`,
    `./render_risk.js` on first click of each tab. Each such module is expected to
    `export async function render(container: HTMLElement): Promise<void>`. A tab
    whose module doesn't exist yet (Tasks 10-14 haven't landed) shows the same
    "Couldn't load this section" error as a real fetch failure — this is intentional,
    not a bug, and is what Step 3's manual verification checks for.
- Consumed by: Tasks 10, 12, 13, 14 (each creates one `render_*.js` this shell will
  pick up automatically, no change to `app.js` needed).

- [ ] **Step 1: Create `viz/util.js`**

```js
export async function fetchJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

export function showError(panelId, err) {
  document.getElementById(panelId).innerHTML =
    `<p class="error">Couldn't load this section: ${err.message}</p>`;
}
```

- [ ] **Step 2: Create `viz/app.js`**

```js
import { showError } from "./util.js";

const TABS = ["overview", "capacity", "constituents", "risk"];

const RENDER_MODULES = {
  overview: "./render_overview.js",
  capacity: "./render_capacity.js",
  constituents: "./render_constituents.js",
  risk: "./render_risk.js",
};

const loaded = new Set();

async function loadTab(name) {
  if (loaded.has(name)) return;
  loaded.add(name);
  const panelId = `tab-${name}`;
  try {
    const mod = await import(RENDER_MODULES[name]);
    await mod.render(document.getElementById(panelId));
  } catch (err) {
    showError(panelId, err);
  }
}

function showTab(name) {
  for (const tab of TABS) {
    const isActive = tab === name;
    document.getElementById(`tab-${tab}`).classList.toggle("active", isActive);
    const button = document.querySelector(`.tab-button[data-tab="${tab}"]`);
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-selected", String(isActive));
  }
  loadTab(name);
}

for (const button of document.querySelectorAll(".tab-button")) {
  button.addEventListener("click", () => showTab(button.dataset.tab));
}

showTab("overview");
```

- [ ] **Step 3: Create `viz/index.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>miniftse — index visualizer</title>
  <link rel="stylesheet" href="style.css" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
</head>
<body>
  <header class="topbar">
    <h1>miniftse</h1>
    <p class="subtitle">A rules-based index platform — reference build, 2016–2026.</p>
  </header>

  <nav class="tabs" role="tablist">
    <button class="tab-button active" data-tab="overview" role="tab" aria-selected="true">Overview</button>
    <button class="tab-button" data-tab="capacity" role="tab" aria-selected="false">Capacity</button>
    <button class="tab-button" data-tab="constituents" role="tab" aria-selected="false">Constituents</button>
    <button class="tab-button" data-tab="risk" role="tab" aria-selected="false">Risk &amp; Attribution</button>
  </nav>

  <main>
    <section id="tab-overview" class="tab-panel active"></section>
    <section id="tab-capacity" class="tab-panel"></section>
    <section id="tab-constituents" class="tab-panel"></section>
    <section id="tab-risk" class="tab-panel"></section>
  </main>

  <script src="app.js" type="module"></script>
</body>
</html>
```

- [ ] **Step 4: Create `viz/style.css`**

```css
:root {
  --bg: #f7f8fa;
  --surface: #ffffff;
  --border: #e2e5ea;
  --text: #1a1d23;
  --text-muted: #5b6472;
  --accent: #2454ff;
  --error: #c62828;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
}

.topbar { padding: 2rem 2rem 1rem; }
.topbar h1 { margin: 0; font-size: 1.75rem; }
.subtitle { color: var(--text-muted); margin: 0.25rem 0 0; }

.tabs {
  display: flex;
  gap: 0.5rem;
  padding: 0 2rem;
  border-bottom: 1px solid var(--border);
}

.tab-button {
  background: none;
  border: none;
  padding: 0.75rem 1rem;
  font-size: 0.95rem;
  color: var(--text-muted);
  cursor: pointer;
  border-bottom: 2px solid transparent;
}

.tab-button.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
  font-weight: 600;
}

main {
  padding: 1.5rem 2rem 3rem;
  max-width: 1100px;
  margin: 0 auto;
}

.tab-panel { display: none; }
.tab-panel.active { display: block; }

.stat-tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 1rem;
  margin-bottom: 1.5rem;
}

.stat-tile {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.stat-value { font-size: 1.5rem; font-weight: 700; }
.stat-label { font-size: 0.8rem; color: var(--text-muted); }

table {
  width: 100%;
  border-collapse: collapse;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  margin-bottom: 1.5rem;
}

th, td {
  text-align: left;
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--border);
  font-size: 0.9rem;
}

th { background: var(--bg); cursor: pointer; user-select: none; }

.error { color: var(--error); }

.slider-row {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin: 1rem 0;
}

input[type="range"] { flex: 1; }

input[type="search"] {
  width: 100%;
  padding: 0.5rem;
  margin-bottom: 1rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 0.9rem;
}
```

- [ ] **Step 5: Manually verify the shell in a browser**

Run: `cd viz && python -m http.server 8000` (plain Python static server — no npm
needed), then open `http://localhost:8000/` in a browser.

Expected:
- Page loads with the "miniftse" header and four tabs, Overview active by default.
- Overview panel shows "Couldn't load this section: ./render_overview.js: ..." (the
  module doesn't exist yet — this is the correct, expected state before Task 10).
- Clicking each of the other three tabs highlights it (underline + colored text) and
  shows the same kind of "couldn't load" message for that tab.
- Open the browser console: no uncaught errors besides the expected 404s for the
  render modules — `app.js` itself must not throw.

Stop the server (Ctrl+C) once verified.

- [ ] **Step 6: Commit**

```bash
git add viz/index.html viz/style.css viz/util.js viz/app.js
git commit -m "viz: static frontend shell with tab navigation and error handling"
```

---

## Task 10: Overview tab — `render_overview.js`

**Files:**
- Create: `viz/render_overview.js`

**Interfaces:**
- Consumes: `fetchJSON`, `showError` (Task 9, `util.js`); `viz/data/overview.json`
  (Task 8's schema); the global `Chart` (Chart.js, loaded via CDN in `index.html`).
- Produces: `export async function render(container: HTMLElement): Promise<void>`
  (the contract `app.js` expects, per Task 9).

- [ ] **Step 1: Create `viz/render_overview.js`**

```js
import { fetchJSON, showError } from "./util.js";

export async function render(container) {
  let data;
  try {
    data = await fetchJSON("./data/overview.json");
  } catch (err) {
    showError(container.id, err);
    return;
  }

  const { stats } = data;
  container.innerHTML = `
    <div class="stat-tiles">
      <div class="stat-tile">
        <span class="stat-value">${(stats.annualised_return * 100).toFixed(1)}%</span>
        <span class="stat-label">Annualised return (GTR)</span>
      </div>
      <div class="stat-tile">
        <span class="stat-value">${(stats.annualised_vol * 100).toFixed(1)}%</span>
        <span class="stat-label">Annualised volatility</span>
      </div>
      <div class="stat-tile">
        <span class="stat-value">${(stats.max_drawdown * 100).toFixed(1)}%</span>
        <span class="stat-label">Maximum drawdown</span>
      </div>
      <div class="stat-tile">
        <span class="stat-value">${stats.divisor_events.toLocaleString()}</span>
        <span class="stat-label">Divisor events</span>
      </div>
    </div>
    <canvas id="overview-chart" height="100"></canvas>
  `;

  new Chart(document.getElementById("overview-chart"), {
    type: "line",
    data: {
      labels: data.dates,
      datasets: [
        { label: "Price return", data: data.pr, borderWidth: 1, pointRadius: 0 },
        { label: "Gross total return", data: data.gtr, borderWidth: 1, pointRadius: 0 },
        { label: "Net total return", data: data.ntr, borderWidth: 1, pointRadius: 0 },
      ],
    },
    options: {
      responsive: true,
      animation: false,
      scales: { x: { ticks: { maxTicksLimit: 10 } } },
    },
  });
}
```

- [ ] **Step 2: Manually verify**

Prerequisite: `viz/data/overview.json` must exist (Task 8, Step 6).

Run: `cd viz && python -m http.server 8000`, open `http://localhost:8000/`.

Expected: Overview tab (active by default) shows four stat tiles with real numbers
(compare against `artefacts/factsheet.md`'s reported annualised return/vol/max
drawdown — they should be close; not necessarily identical if `factsheet.md` was
generated against a different date range than the current `levels.parquet`), and a
line chart with three series (PR/GTR/NTR) spanning the index's full history. No
console errors.

- [ ] **Step 3: Commit**

```bash
git add viz/render_overview.js
git commit -m "viz: render the Overview tab (stat tiles + level history chart)"
```

---

## Task 11: `capacity.js` — ported capacity-trim algorithm, Node-tested

**Files:**
- Create: `viz/capacity.js`
- Create: `viz/capacity.test.mjs`

**Interfaces:**
- Produces:
  - `capacityConstrainedWeights(constituents: {security_id, weight, adv}[], fundSize:
    number, opts?: {maxDaysToTrade?: number, participation?: number}): {weights:
    Record<string, number>, trimmed: string[]}` — a direct port of
    `weighting.schemes.capacity_constrained_weights`
    (`src/miniftse/weighting/schemes.py:184-225`).
  - `weightedAverageDaysToTrade(weights: Record<string, number>, constituents:
    {security_id, adv}[], fundSize: number, participation?: number): number` — a
    direct port of `weighting.schemes.weighted_average_days_to_trade`
    (`src/miniftse/weighting/schemes.py:242-253`).
- Consumed by: Task 12 (`render_capacity.js`). Pure logic, no DOM access — this is
  what makes it testable with plain Node (no browser, no npm package).

- [ ] **Step 1: Write the failing tests**

Create `viz/capacity.test.mjs`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { capacityConstrainedWeights, weightedAverageDaysToTrade } from "./capacity.js";

const TRIM_FIXTURE = [
  { security_id: "A", weight: 0.6, adv: 100_000 },
  { security_id: "B", weight: 0.3, adv: 1_000_000_000 },
  { security_id: "C", weight: 0.1, adv: 1_000_000_000 },
];

test("fund_size <= 0 returns weights unchanged, nothing trimmed", () => {
  const { weights, trimmed } = capacityConstrainedWeights(TRIM_FIXTURE, 0);
  assert.deepEqual(weights, { A: 0.6, B: 0.3, C: 0.1 });
  assert.deepEqual(trimmed, []);
});

test("an illiquid name gets trimmed and the residual redistributes", () => {
  // ceiling(A) = adv * participation * max_days / fund_size
  //            = 100_000 * 0.20 * 5.0 / 100_000_000 = 0.001 < A's 0.6 weight -> trimmed
  // ceiling(B) = ceiling(C) = 1_000_000_000 * 0.20 * 5.0 / 100_000_000 = 10.0,
  //   far above B/C's weights -> untouched, absorb A's freed 0.599 pro-rata (3:1)
  const { weights, trimmed } = capacityConstrainedWeights(TRIM_FIXTURE, 100_000_000);
  assert.deepEqual(trimmed, ["A"]);
  assert.ok(Math.abs(weights.A - 0.001) < 1e-9, `A=${weights.A}`);
  assert.ok(Math.abs(weights.B - 0.74925) < 1e-9, `B=${weights.B}`);
  assert.ok(Math.abs(weights.C - 0.24975) < 1e-9, `C=${weights.C}`);
  const total = Object.values(weights).reduce((s, v) => s + v, 0);
  assert.ok(Math.abs(total - 1) < 1e-9);
});

test("weightedAverageDaysToTrade matches the days-to-trade formula", () => {
  const constituents = [
    { security_id: "A", adv: 1_000_000 },
    { security_id: "B", adv: 10_000_000 },
    { security_id: "C", adv: 10_000_000 },
  ];
  const weights = { A: 0.6, B: 0.3, C: 0.1 };
  const result = weightedAverageDaysToTrade(weights, constituents, 10_000_000);
  // days-to-trade: A = 10e6*0.6/(1e6*0.2)=30; B = 10e6*0.3/(10e6*0.2)=1.5;
  // C = 10e6*0.1/(10e6*0.2)=0.5; weighted by weight: 0.6*30+0.3*1.5+0.1*0.5=18.5
  assert.ok(Math.abs(result - 18.5) < 1e-9, `got ${result}`);
});

test("zero-weight positions don't distort the weighted average", () => {
  const constituents = [{ security_id: "A", adv: 0 }, { security_id: "B", adv: 1_000_000 }];
  const weights = { A: 0.0, B: 1.0 };
  const result = weightedAverageDaysToTrade(weights, constituents, 1_000_000);
  // A has adv=0 -> excluded from the average entirely; B: 1e6*1.0/(1e6*0.2)=5
  assert.ok(Math.abs(result - 5.0) < 1e-9, `got ${result}`);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test viz/capacity.test.mjs`
Expected: FAIL — `Cannot find module './capacity.js'`

- [ ] **Step 3: Create `viz/capacity.js`**

```js
/** Direct JS port of weighting.schemes.capacity_constrained_weights /
 * weighted_average_days_to_trade (src/miniftse/weighting/schemes.py:184-253).
 * Pure functions, no DOM - lets the fund-size slider recompute entirely
 * client-side, and lets this file be tested with plain `node --test`. */

export function capacityConstrainedWeights(constituents, fundSize, opts = {}) {
  const { maxDaysToTrade = 5.0, participation = 0.20 } = opts;

  const base = {};
  const adv = {};
  for (const c of constituents) {
    base[c.security_id] = c.weight;
    adv[c.security_id] = c.adv;
  }

  if (fundSize <= 0) {
    return { weights: { ...base }, trimmed: [] };
  }

  const ceilings = {};
  for (const id in base) {
    ceilings[id] = adv[id] > 0
      ? (adv[id] * participation * maxDaysToTrade) / fundSize
      : 0.0;
  }

  let w = { ...base };
  const frozen = new Set();
  for (let iter = 0; iter < 100; iter++) {
    const breaching = Object.keys(w).filter(k => !frozen.has(k) && w[k] > ceilings[k]);
    if (breaching.length === 0) break;
    for (const k of breaching) {
      w[k] = ceilings[k];
      frozen.add(k);
    }
    const residual = 1.0 - [...frozen].reduce((s, k) => s + w[k], 0);
    const free = Object.keys(w).filter(k => !frozen.has(k));
    const freeMass = free.reduce((s, k) => s + w[k], 0);
    if (freeMass <= 0 || residual <= 0) break;
    for (const k of free) {
      w[k] *= residual / freeMass;
    }
  }

  const total = Object.values(w).reduce((s, v) => s + v, 0);
  const normalised = {};
  for (const k in w) normalised[k] = total > 0 ? w[k] / total : 0;
  return { weights: normalised, trimmed: [...frozen] };
}

export function weightedAverageDaysToTrade(weights, constituents, fundSize, participation = 0.20) {
  let numerator = 0;
  let denominator = 0;
  for (const c of constituents) {
    const w = weights[c.security_id] ?? 0;
    if (c.adv > 0 && w > 0) {
      const dtt = (fundSize * w) / (c.adv * participation);
      numerator += w * dtt;
      denominator += w;
    }
  }
  return denominator > 0 ? numerator / denominator : Infinity;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test viz/capacity.test.mjs`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add viz/capacity.js viz/capacity.test.mjs
git commit -m "viz: port the capacity-trim algorithm to JS, Node-tested"
```

---

## Task 12: Capacity tab — `render_capacity.js`

**Files:**
- Create: `viz/render_capacity.js`

**Interfaces:**
- Consumes: `fetchJSON`, `showError` (Task 9); `capacityConstrainedWeights`,
  `weightedAverageDaysToTrade` (Task 11); `viz/data/capacity.json` (Task 8's schema).
- Produces: `export async function render(container): Promise<void>`.

- [ ] **Step 1: Create `viz/render_capacity.js`**

```js
import { fetchJSON, showError } from "./util.js";
import { capacityConstrainedWeights, weightedAverageDaysToTrade } from "./capacity.js";

const SCHEME_LABELS = {
  float_market_cap: "Float market cap",
  equal: "Equal weight",
  fundamental: "Fundamental",
  score_tilt: "Score tilt",
  selection: "Selection",
  optimised: "Optimised",
};

const SCHEME_COLUMNS = ["turnover", "capacity", "factor_exposure", "explainability", "use_when"];

function renderSchemeTable(schemes) {
  const headerCells = ["Scheme", ...SCHEME_COLUMNS.map(c => c.replace(/_/g, " "))]
    .map(h => `<th>${h}</th>`).join("");
  const rows = Object.entries(schemes).map(([key, props]) => {
    const cells = SCHEME_COLUMNS.map(c => `<td>${props[c]}</td>`).join("");
    return `<tr><td>${SCHEME_LABELS[key] ?? key}</td>${cells}</tr>`;
  }).join("");
  return `<table><thead><tr>${headerCells}</tr></thead><tbody>${rows}</tbody></table>`;
}

function fmtMoney(value) {
  if (value >= 1e9) return `$${(value / 1e9).toFixed(1)}bn`;
  if (value >= 1e6) return `$${(value / 1e6).toFixed(0)}m`;
  return `$${value.toFixed(0)}`;
}

export async function render(container) {
  let data;
  try {
    data = await fetchJSON("./data/capacity.json");
  } catch (err) {
    showError(container.id, err);
    return;
  }

  const { schemes, constituents, capacity_params } = data;

  container.innerHTML = `
    <h2>Six weighting schemes, three trade-offs</h2>
    ${renderSchemeTable(schemes)}

    <h2>Fund-size capacity, on the current constituent snapshot</h2>
    <div class="slider-row">
      <label for="fund-size">Fund size</label>
      <input type="range" id="fund-size" min="0" max="9" step="0.05" value="2">
      <span id="fund-size-label"></span>
    </div>
    <div class="stat-tiles">
      <div class="stat-tile">
        <span class="stat-value" id="names-trimmed">-</span>
        <span class="stat-label">Names trimmed</span>
      </div>
      <div class="stat-tile">
        <span class="stat-value" id="avg-days">-</span>
        <span class="stat-label">Weighted avg. days to trade</span>
      </div>
    </div>
  `;

  const slider = document.getElementById("fund-size");
  const label = document.getElementById("fund-size-label");
  const trimmedEl = document.getElementById("names-trimmed");
  const daysEl = document.getElementById("avg-days");

  // Log scale: slider 0..9 -> fund size $10m..$10bn. Capacity questions span orders
  // of magnitude, so a linear slider would waste most of its range below $1bn.
  function fundSizeFromSlider() {
    return 10 ** (7 + Number(slider.value));
  }

  function update() {
    const fundSize = fundSizeFromSlider();
    label.textContent = fmtMoney(fundSize);
    const { weights, trimmed } = capacityConstrainedWeights(constituents, fundSize, {
      participation: capacity_params.participation,
      maxDaysToTrade: capacity_params.max_days_to_trade,
    });
    trimmedEl.textContent = trimmed.length;
    const avgDays = weightedAverageDaysToTrade(
      weights, constituents, fundSize, capacity_params.participation
    );
    daysEl.textContent = Number.isFinite(avgDays) ? avgDays.toFixed(1) : "∞";
  }

  slider.addEventListener("input", update);
  update();
}
```

- [ ] **Step 2: Manually verify**

Prerequisite: `viz/data/capacity.json` exists (Task 8) with real (non-zero) `adv`
values (Task 8 Step 6's re-seed check).

Run: `cd viz && python -m http.server 8000`, open `http://localhost:8000/`, click
"Capacity".

Expected:
- The six-scheme trade-off table renders with real text from `SCHEME_PROPERTIES`
  (e.g. "Equal weight" row's capacity column reads "low - bounded by the smallest
  constituent").
- Dragging the fund-size slider updates the "$" label, "Names trimmed", and "Weighted
  avg. days to trade" tiles live, with no page reload.
- At the far left of the slider (smallest fund size), "Names trimmed" should be 0 or
  very low; at the far right ($10bn), it should be meaningfully higher — if it's flat
  across the whole range, the ADV data likely wasn't picked up correctly (re-check
  Task 8 Step 6's re-seed prerequisite).

- [ ] **Step 3: Commit**

```bash
git add viz/render_capacity.js
git commit -m "viz: render the Capacity tab (trade-off table + live fund-size slider)"
```

---

## Task 13: Constituents tab — `render_constituents.js`

**Files:**
- Create: `viz/render_constituents.js`

**Interfaces:**
- Consumes: `fetchJSON`, `showError` (Task 9); `viz/data/constituents.json`
  (Task 8's schema).
- Produces: `export async function render(container): Promise<void>`.

- [ ] **Step 1: Create `viz/render_constituents.js`**

```js
import { fetchJSON, showError } from "./util.js";

function renderTable(rows) {
  const header = `<tr>
    <th data-key="security_id">Security</th>
    <th data-key="weight">Weight</th>
    <th data-key="sector">Sector</th>
    <th data-key="country">Country</th>
    <th data-key="size_band">Size band</th>
  </tr>`;
  const body = rows.map(r => `<tr>
    <td>${r.security_id}</td>
    <td>${(r.weight * 100).toFixed(2)}%</td>
    <td>${r.sector}</td>
    <td>${r.country}</td>
    <td>${r.size_band}</td>
  </tr>`).join("");
  return `<table><thead>${header}</thead><tbody>${body}</tbody></table>`;
}

export async function render(container) {
  let data;
  try {
    data = await fetchJSON("./data/constituents.json");
  } catch (err) {
    showError(container.id, err);
    return;
  }

  const rows = data.constituents;
  let sortKey = "weight";
  let sortDesc = true;

  container.innerHTML = `
    <h2>Constituents as of ${data.as_of} (${rows.length} names)</h2>
    <input type="search" id="constituent-filter" placeholder="Filter by security, sector or country…">
    <div id="constituent-table"></div>
  `;

  const tableDiv = document.getElementById("constituent-table");
  const filterInput = document.getElementById("constituent-filter");

  function draw() {
    const q = filterInput.value.trim().toLowerCase();
    const filtered = q
      ? rows.filter(r =>
          r.security_id.toLowerCase().includes(q) ||
          r.country.toLowerCase().includes(q) ||
          r.sector.toLowerCase().includes(q))
      : rows;
    const sorted = [...filtered].sort((a, b) => {
      const [x, y] = [a[sortKey], b[sortKey]];
      const cmp = typeof x === "number" ? x - y : String(x).localeCompare(String(y));
      return sortDesc ? -cmp : cmp;
    });
    tableDiv.innerHTML = renderTable(sorted);
    for (const th of tableDiv.querySelectorAll("th[data-key]")) {
      th.addEventListener("click", () => {
        const key = th.dataset.key;
        sortDesc = key === sortKey ? !sortDesc : true;
        sortKey = key;
        draw();
      });
    }
  }

  filterInput.addEventListener("input", draw);
  draw();
}
```

- [ ] **Step 2: Manually verify**

Prerequisite: `viz/data/constituents.json` exists (Task 8).

Run: `cd viz && python -m http.server 8000`, open `http://localhost:8000/`, click
"Constituents".

Expected: a table of all constituents, initially sorted by weight descending; typing
in the filter box narrows the rows by security id/country/sector; clicking a column
header re-sorts by that column, clicking the same header again reverses the sort
direction.

- [ ] **Step 3: Commit**

```bash
git add viz/render_constituents.js
git commit -m "viz: render the Constituents tab (sortable, filterable table)"
```

---

## Task 14: Risk & Attribution tab — `render_risk.js`

**Files:**
- Create: `viz/render_risk.js`

**Interfaces:**
- Consumes: `fetchJSON`, `showError` (Task 9); `viz/data/risk_attribution.json`
  (Task 7's `parse_onepager` schema, `{risk: {...}, attribution: {...}}`); the global
  `Chart`.
- Produces: `export async function render(container): Promise<void>`.

- [ ] **Step 1: Create `viz/render_risk.js`**

```js
import { fetchJSON, showError } from "./util.js";

function renderSection(section) {
  const table = section.table
    ? `<table><thead><tr>${section.table.columns.map(c => `<th>${c}</th>`).join("")}</tr></thead>
       <tbody>${section.table.rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`
    : "";
  return `<h3>${section.heading}</h3><p>${section.text}</p>${table}`;
}

function renderOnepager(doc) {
  return `
    <h2>${doc.title}</h2>
    <p class="subtitle">${doc.meta}</p>
    ${doc.sections.map(renderSection).join("")}
  `;
}

function findTable(doc, headingSubstring) {
  const section = doc.sections.find(
    s => s.heading.toLowerCase().includes(headingSubstring)
  );
  return section ? section.table : null;
}

export async function render(container) {
  let data;
  try {
    data = await fetchJSON("./data/risk_attribution.json");
  } catch (err) {
    showError(container.id, err);
    return;
  }

  container.innerHTML = `
    <div class="risk-section">${renderOnepager(data.risk)}</div>
    <canvas id="risk-chart" height="80"></canvas>
    <div class="attribution-section">${renderOnepager(data.attribution)}</div>
  `;

  const riskTable = findTable(data.risk, "where the risk comes from");
  if (riskTable) {
    const shareIdx = riskTable.columns.findIndex(c => c.toLowerCase().includes("share"));
    new Chart(document.getElementById("risk-chart"), {
      type: "bar",
      data: {
        labels: riskTable.rows.map(r => r[0]),
        datasets: [{
          label: "Share of risk",
          data: riskTable.rows.map(r => parseFloat(r[shareIdx])),
        }],
      },
      options: { responsive: true, animation: false },
    });
  }
}
```

- [ ] **Step 2: Manually verify**

Prerequisite: `viz/data/risk_attribution.json` exists (Task 8).

Run: `cd viz && python -m http.server 8000`, open `http://localhost:8000/`, click
"Risk & Attribution".

Expected: the risk one-pager's title/meta/sections render as headed prose blocks with
tables where present, a bar chart of "share of risk" by factor appears between the two
one-pagers, and the attribution one-pager renders below it the same way. If the bar
chart doesn't appear, check the browser console — the most likely cause is
`findTable`'s heading substring match failing against the real file's exact heading
text (verify with the `parse_onepager` sanity check from Task 7, Step 5).

- [ ] **Step 3: Commit**

```bash
git add viz/render_risk.js
git commit -m "viz: render the Risk & Attribution tab (parsed one-pagers + risk chart)"
```

---

## Task 15: GitHub Pages deployment

**Files:**
- Create: `.github/workflows/pages.yml`
- Modify: `README.md` (add a link to the deployed site)

**Interfaces:** none — this is the deployment step; nothing downstream depends on it.

- [ ] **Step 1: Create `.github/workflows/pages.yml`**

```yaml
name: Deploy viz to GitHub Pages

on:
  push:
    branches: [main]
    paths: ["viz/**"]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: viz
      - id: deployment
        uses: actions/deploy-pages@v4
```

This workflow is deliberately separate from `.github/workflows/ci.yml` and does not
run `export.py` — per the design spec, `export.py` needs the full `miniftse` Python
environment and is a low-frequency step (`make viz`, committed like `artefacts/`
already is), not something to run on every push.

- [ ] **Step 2: One-time manual step — enable Pages**

In the GitHub repo's Settings → Pages, set "Source" to "GitHub Actions" (this cannot
be done from a workflow file; it's a one-time repo setting). Note this for whoever
merges this branch if you don't have push/admin access to do it yourself as part of
this task.

- [ ] **Step 3: Add a link from the README**

Near the top of `README.md`, after the existing `make setup && make test &&
make build-index` code block (`README.md:11-13`), add:

```markdown
A live build is browsable at **https://<github-username>.github.io/miniftse/**
(source: `viz/`).
```

Replace `<github-username>` with the actual GitHub username/org this repo is hosted
under before committing — check `git remote get-url origin` for the real path if
unsure.

- [ ] **Step 4: Verify the workflow syntax**

Run: `uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/pages.yml'))"`

(If `pyyaml` isn't a project dependency, `python -c "import yaml"` will fail with
`ModuleNotFoundError` — in that case just visually re-check the YAML indentation
against the block above instead of trying to install a new dependency for a one-off
syntax check.)

Expected: no exception, or an acceptable fallback to visual inspection per the note
above.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/pages.yml README.md
git commit -m "viz: deploy to GitHub Pages via GitHub Actions"
```

- [ ] **Step 6: Push and confirm the live deployment** (only once the user is ready —
  this is the first step in this whole plan that touches the remote/public repo state)

This step is a checkpoint, not something to run automatically: pushing to `main`
triggers the real, public deployment. Confirm with the user before running `git push`,
then check the Actions tab for the `Deploy viz to GitHub Pages` run, and once it
succeeds, open the Pages URL and click through all four tabs one more time against the
live, deployed copy.

---

## Self-Review Notes

- **Spec coverage:** every section of `docs/superpowers/specs/2026-08-11-capacity-viz-design.md`
  maps to a task — Data pipeline → Tasks 1-4; `viz/export.py` → Tasks 5-8; Frontend →
  Tasks 9-14; Deployment → Task 15. The three "Open items" the spec handed off are all
  resolved: the ADV wiring point is Tasks 1-4 (found by reading the actual code, not
  guessed), Chart.js is pinned to `4.4.4` (Task 9), and the annualised-return/vol/
  max-drawdown formulas are pinned in Task 5 (CAGR over trading days, sample stdev of
  daily returns annualised by `sqrt(252)`, peak-to-trough on the GTR series).
- **No placeholders:** every step has real code or a concrete, runnable verification
  command. Two spots initially drafted from an incomplete read of the source (Task 2's
  `Spinoff` fields, Task 3's reconstitution fixture) were corrected against the actual
  class definitions (`corpactions/events.py:402-412`, `production/build.py:95-124`)
  before this plan was finalized, rather than left as guesses.
- **Type/name consistency:** `Constituent.adv` / `ConstituentSpec.adv` (Task 1) is
  referenced with the same name through Tasks 2-4; `build_constituents`/
  `build_capacity`/`parse_onepager`/`main` (Tasks 5-8) are used with matching
  signatures by each other; `fetchJSON`/`showError` (Task 9) and `render(container)`
  (Tasks 10, 12-14) and `capacityConstrainedWeights`/`weightedAverageDaysToTrade`
  (Tasks 11-12) are consistent across every task that references them.
