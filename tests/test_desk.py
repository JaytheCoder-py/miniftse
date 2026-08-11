"""Tests for the ops desk: context persistence, snapshot build, and the app."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pandas as pd
import pytest

from miniftse.config import global_all_cap
from miniftse.quality.rules import ValidationContext


def _context() -> ValidationContext:
    as_of = dt.date(2020, 6, 30)
    prices = pd.DataFrame({
        "security_id": ["S1", "S2"], "date": [as_of, as_of], "close": [10.0, 20.0],
    })
    return ValidationContext(
        as_of=as_of,
        prices=prices,
        prior_prices=prices.assign(close=[9.5, 20.5]),
        weights=pd.Series({"S1": 0.6, "S2": 0.4}),
        shares=pd.DataFrame({"security_id": ["S1", "S2"], "shares": [100.0, 50.0]}),
        divisor=1234.5,
        prior_divisor=1230.0,
        index_level=1000.0,
        prior_index_level=998.0,
        total_market_value=1_234_500.0,
        constituents={"S1": None, "S2": None},
    )


def test_validation_context_round_trips(tmp_path):
    ctx = _context()
    ctx.save(tmp_path / "baseline")
    back = ValidationContext.load(tmp_path / "baseline")

    assert back.as_of == ctx.as_of
    assert back.divisor == ctx.divisor
    assert back.prior_divisor == ctx.prior_divisor
    assert back.index_level == ctx.index_level
    assert back.total_market_value == ctx.total_market_value
    assert set(back.constituents) == set(ctx.constituents)
    pd.testing.assert_frame_equal(back.prices, ctx.prices)
    pd.testing.assert_series_equal(back.weights, ctx.weights)


def test_validation_context_round_trips_none_fields(tmp_path):
    """Absent frames must come back absent, not as empty DataFrames.

    An empty frame and a missing frame mean different things to the rules: several
    checks in `quality/checks.py` short-circuit to a pass when their input is None and
    would report a spurious failure against an empty frame.
    """
    ctx = _context()
    ctx.save(tmp_path / "b")
    back = ValidationContext.load(tmp_path / "b")
    assert back.corp_actions is None
    assert back.alternate_source is None
    assert back.fx is None


def test_loaded_context_passes_the_same_rules(tmp_path):
    """The real acceptance criterion: same findings before and after a round trip."""
    from miniftse.quality.rules import ValidationEngine

    ctx = _context()
    engine = ValidationEngine.default()
    before = engine.run(ctx, run_id="before").to_frame()
    ctx.save(tmp_path / "b")
    after = engine.run(ValidationContext.load(tmp_path / "b"), run_id="after").to_frame()

    pd.testing.assert_frame_equal(
        before.drop(columns=["run_id"], errors="ignore"),
        after.drop(columns=["run_id"], errors="ignore"),
    )


def test_validation_context_round_trips_remaining_frames_and_config(tmp_path):
    """The fields `_context()` leaves at their default: the frames it doesn't set, the
    `official_level` scalar, and `config` resolved by name through the constructors in
    `miniftse.config`.
    """
    as_of = dt.date(2020, 6, 30)
    ctx = _context()
    ctx.corp_actions = pd.DataFrame({"security_id": ["S1"], "action": ["SPLIT"]})
    ctx.divisor_audit = pd.DataFrame({"date": [as_of], "divisor": [1234.5]})
    ctx.reference = pd.DataFrame({"security_id": ["S1", "S2"], "country": ["US", "GB"]})
    ctx.alternate_source = pd.DataFrame({"security_id": ["S1"], "close": [10.1]})
    ctx.fx = pd.DataFrame({"base": ["USD"], "quote": ["GBP"], "rate": [0.8]})
    ctx.prior_fx = pd.DataFrame({"base": ["USD"], "quote": ["GBP"], "rate": [0.79]})
    ctx.official_level = 999.5
    ctx.config = global_all_cap()

    ctx.save(tmp_path / "full")
    back = ValidationContext.load(tmp_path / "full")

    pd.testing.assert_frame_equal(back.corp_actions, ctx.corp_actions)
    pd.testing.assert_frame_equal(back.divisor_audit, ctx.divisor_audit)
    pd.testing.assert_frame_equal(back.reference, ctx.reference)
    pd.testing.assert_frame_equal(back.alternate_source, ctx.alternate_source)
    pd.testing.assert_frame_equal(back.fx, ctx.fx)
    pd.testing.assert_frame_equal(back.prior_fx, ctx.prior_fx)
    assert back.official_level == ctx.official_level
    assert back.config == ctx.config


def test_validation_context_save_rejects_config_not_built_by_name(tmp_path):
    """`config` only round-trips as a name resolved through `miniftse.config`'s named
    constructors (`global_all_cap` and its siblings). A hand-built IndexConfig can't be
    identified by name, so save() must fail loudly rather than silently drop it.
    """
    ctx = _context()
    ctx.config = replace(global_all_cap(), index_id="CUSTOM")

    with pytest.raises(ValueError, match="not one of the named constructors"):
        ctx.save(tmp_path / "bad-config")


# --------------------------------------------------------------------------------------
# Task 2: the snapshot build
# --------------------------------------------------------------------------------------


@pytest.mark.slow
def test_build_snapshot_writes_every_expected_file(tmp_path):
    from miniftse.data.synthetic import SyntheticConfig
    from miniftse.desk.snapshot import EXPECTED_FILES, build_snapshot
    from miniftse.production.build import BuildSpec

    spec = BuildSpec(
        universe_config=SyntheticConfig(n_securities=100, seed=20260809),
        start=dt.date(2019, 1, 2), end=dt.date(2019, 12, 31),
    )
    build_snapshot(tmp_path, spec)
    for name in EXPECTED_FILES:
        assert (tmp_path / name).exists(), f"snapshot did not write {name}"


def test_build_snapshot_fails_loudly_on_missing_artefact(tmp_path, monkeypatch):
    """A partial snapshot must never be written."""
    from miniftse.desk import snapshot as snap
    monkeypatch.setattr(snap, "_read_onepagers", lambda *a, **k: (_ for _ in ()).throw(
        FileNotFoundError("risk_onepager.md")))
    with pytest.raises(FileNotFoundError, match="risk_onepager"):
        snap.build_snapshot(tmp_path)
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.slow
def test_failed_rerun_leaves_no_stale_manifest(tmp_path, monkeypatch):
    """A rerun writes in place, so the completeness signal must not survive a failure.

    Without this, a rerun that dies after `days.parquet` is overwritten leaves the
    previous run's `manifest.json` over a directory that still contains every name in
    EXPECTED_FILES - and the startup loader happily serves days.parquet from build B
    beside overview.json from build A. A mixed snapshot passes every existence check,
    which is exactly why the existence check cannot be the only guard.
    """
    from miniftse.data.synthetic import SyntheticConfig
    from miniftse.desk import snapshot as snap
    from miniftse.production.build import BuildSpec

    spec = BuildSpec(
        universe_config=SyntheticConfig(n_securities=100, seed=20260809),
        start=dt.date(2019, 1, 2), end=dt.date(2019, 12, 31),
    )
    snap.build_snapshot(tmp_path, spec)
    assert (tmp_path / "manifest.json").exists()

    # Fail at a late artefact, after the parquet files have already been rewritten.
    monkeypatch.setattr(snap, "_evals_payload", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("the eval harness fell over")))
    with pytest.raises(RuntimeError, match="eval harness"):
        snap.build_snapshot(tmp_path, spec)

    assert not (tmp_path / "manifest.json").exists()
    assert (tmp_path / "days.parquet").exists(), (
        "the test is only meaningful if the rerun got far enough to overwrite an "
        "artefact before failing"
    )


# --------------------------------------------------------------------------------------
# Task 3: the FastAPI skeleton
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def desk_data_dir(tmp_path_factory):
    """One snapshot, built once, shared by every test in this section.

    `client` and `test_startup_is_under_two_seconds` both need a real snapshot on disk
    but must not each pay to build one - the startup-time test in particular has to
    measure `create_app`/`TestClient` alone, not a 100-security build hiding inside it.
    """
    from miniftse.data.synthetic import SyntheticConfig
    from miniftse.desk.snapshot import build_snapshot
    from miniftse.production.build import BuildSpec

    data = tmp_path_factory.mktemp("desk-data")
    build_snapshot(data, BuildSpec(
        universe_config=SyntheticConfig(n_securities=100, seed=20260809),
        start=dt.date(2019, 1, 2), end=dt.date(2019, 12, 31),
    ))
    return data


@pytest.fixture(scope="module")
def client(desk_data_dir):
    from fastapi.testclient import TestClient

    from miniftse.desk.app import create_app

    with TestClient(create_app(data_dir=desk_data_dir)) as c:
        yield c


@pytest.mark.slow
def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["snapshot_git_sha"]


@pytest.mark.slow
def test_root_redirects_to_day(client):
    assert client.get("/", follow_redirects=False).status_code in (302, 307)


@pytest.mark.slow
def test_startup_is_under_two_seconds(desk_data_dir):
    """A hiring manager gives the page ninety seconds. Startup is not where they go."""
    import time

    from fastapi.testclient import TestClient

    from miniftse.desk.app import create_app

    # Reuse the module-scoped snapshot directory rather than rebuilding: only the
    # `TestClient`/lifespan cost inside this block counts against the budget.
    data = desk_data_dir
    start = time.perf_counter()
    with TestClient(create_app(data_dir=data)):
        pass
    assert time.perf_counter() - start < 2.0


def test_missing_snapshot_refuses_to_start(tmp_path):
    from fastapi.testclient import TestClient

    from miniftse.desk.app import create_app

    with (
        pytest.raises(FileNotFoundError, match="make desk-data"),
        TestClient(create_app(data_dir=tmp_path)),
    ):
        pass


@pytest.mark.slow
def test_404_renders_inside_the_site_layout(client):
    """The design spec's rule: a visitor never sees a raw traceback, 404 included. A
    missing route must come back as the styled site shell - banner and nav - not
    Starlette's bare default error page and not a stack trace.
    """
    response = client.get("/this-route-does-not-exist")
    assert response.status_code == 404
    body = response.text
    assert "simulated universe" in body  # the persistent synthetic-data banner
    assert "Explain a day" in body  # a nav item, i.e. the full base.html layout
    assert "Traceback" not in body


# --------------------------------------------------------------------------------------
# Task 4: `explain_day` / `notable_days`
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def desk_state(desk_data_dir):
    """The service layer needs a `DeskState`, not a running app - `load_desk_state`
    reads the same on-disk snapshot the `client` fixture serves, without paying for a
    FastAPI/TestClient lifespan.
    """
    from miniftse.desk.state import load_desk_state

    return load_desk_state(desk_data_dir)


@pytest.mark.slow
def test_explain_day_no_event_day_is_all_market(desk_state):
    """(a) A date with no divisor events: `events == []` and the narrative says the
    whole move was the market's doing."""
    from miniftse.desk.services import explain_day

    days = desk_state.days
    plain_days = days.loc[(days["n_divisor_events"] == 0) & (~days["is_review"])]
    assert not plain_days.empty, "fixture must contain at least one plain trading day"
    # The last such day, not the first: the first plain day may be the index's
    # inception date, which has no prior session and so a trivially zero move - a
    # weaker exercise of the market-move arithmetic than a genuine mid-series day.
    date = plain_days.iloc[-1]["date"].date()

    explanation = explain_day(desk_state, date)

    assert explanation.events == []
    assert explanation.review is None
    assert explanation.structural_move_bps == 0.0
    assert "market" in explanation.narrative.lower()


@pytest.mark.slow
def test_explain_day_event_date_reports_audit_figures(desk_state):
    """(b) A date with a divisor event: each event dict carries `continuity_error_bps`
    and `realised_return_bps` straight from the audit frame, plus its `apply_order`."""
    from miniftse.desk.services import explain_day

    audit = desk_state.divisor_audit
    corp_events = audit.loc[audit["event_type"] != "REVIEW"]
    assert not corp_events.empty, "fixture must contain at least one corporate action"
    date = corp_events.iloc[0]["date"].date()
    expected = corp_events.loc[corp_events["date"] == pd.Timestamp(date)]

    explanation = explain_day(desk_state, date)

    assert len(explanation.events) == len(expected)
    assert explanation.events
    for event in explanation.events:
        assert "continuity_error_bps" in event
        assert "realised_return_bps" in event
        assert "apply_order" in event
        assert isinstance(event["apply_order"], int)
    assert explanation.divisor_before == pytest.approx(float(expected.iloc[0]["divisor_before"]))


@pytest.mark.slow
def test_explain_day_review_date_carries_turnover(desk_state):
    """(c) A review date's `review` dict carries the review row, turnover included."""
    from miniftse.desk.services import explain_day

    reviews = desk_state.reviews
    assert not reviews.empty, "fixture must contain at least one review"
    row = reviews.iloc[0]
    date = row["date"].date()

    explanation = explain_day(desk_state, date)

    assert explanation.review is not None
    assert explanation.review["one_way_turnover"] == pytest.approx(
        float(row["one_way_turnover"])
    )
    assert explanation.review["n_additions"] == row["n_additions"]


@pytest.mark.slow
def test_explain_day_out_of_index_date_raises_key_error(desk_state):
    """(d) A date outside the index raises `KeyError`, not a lookup that silently
    returns nothing - the route layer turns this into a 400."""
    from miniftse.desk.services import explain_day

    with pytest.raises(KeyError):
        explain_day(desk_state, dt.date(1990, 1, 1))


@pytest.mark.slow
def test_notable_days_covers_all_four_categories(desk_state):
    from miniftse.desk.services import notable_days

    entries = notable_days(desk_state)
    categories = {entry["category"] for entry in entries}
    assert categories == {
        "largest_divisor_event",
        "largest_review_turnover",
        "largest_continuity_error",
        "largest_single_day_move",
    }
    for entry in entries:
        assert isinstance(entry["date"], dt.date)
        assert entry["reason"]


@pytest.mark.slow
def test_500_renders_inside_the_site_layout(desk_data_dir):
    """Same rule for an unhandled exception. A route that always raises is added to a
    throwaway app built over the same snapshot, rather than teaching the real app to
    fail on demand - and `raise_server_exceptions=False` so the client returns the
    response our handler built instead of re-raising `RuntimeError` at the test.
    """
    from fastapi.testclient import TestClient

    from miniftse.desk.app import create_app

    app = create_app(data_dir=desk_data_dir)

    @app.get("/__boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get("/__boom")

    assert response.status_code == 500
    body = response.text
    assert "simulated universe" in body
    assert "Explain a day" in body
    assert "Traceback" not in body
    assert "boom" not in body


# --------------------------------------------------------------------------------------
# Task 5: Screen 1 - `/day` route and template
# --------------------------------------------------------------------------------------


@pytest.mark.slow
def test_available_dates_matches_days_frame(desk_state):
    """The closed set the route validates against: sorted, and exactly the dates
    `days.parquet` published a level for."""
    from miniftse.desk.services import available_dates

    dates = available_dates(desk_state)
    assert dates == sorted(dates)
    assert set(dates) == {row.date() for row in desk_state.days["date"]}


@pytest.mark.slow
def test_day_default_shows_notable_days(client):
    """No `date` param: 200, and the notable-days pinned list is present."""
    response = client.get("/day")
    assert response.status_code == 200
    assert "notable days" in response.text.lower()


@pytest.mark.slow
def test_day_default_selects_most_recent_date(client, desk_state):
    """The unspecified-date default is the most recently published session."""
    most_recent = desk_state.days.sort_values("date").iloc[-1]["date"].date()
    response = client.get("/day")
    assert response.status_code == 200
    assert most_recent.isoformat() in response.text


@pytest.mark.slow
def test_day_known_review_date_shows_turnover(client, desk_state):
    """A review date's page renders the review card (its own `data-testid` marker,
    which only the `{% if explanation.review %}` block can produce) containing the
    review's one-way turnover figure.

    The marker matters: `explanation.narrative` embeds the same `.2%`-formatted
    turnover figure unconditionally (see `services._narrative`), so asserting on the
    turnover string alone would still pass even if the review card itself were
    deleted from the template - it would just be reading the narrative sentence
    instead. The marker isolates "the review block renders" from "the narrative
    mentions turnover".
    """
    from miniftse.desk.services import explain_day

    reviews = desk_state.reviews
    assert not reviews.empty, "fixture must contain at least one review"
    row = reviews.iloc[0]
    date = row["date"].date()
    explanation = explain_day(desk_state, date)
    assert explanation.review is not None

    response = client.get("/day", params={"date": date.isoformat()})

    assert response.status_code == 200
    assert 'data-testid="review-card"' in response.text
    turnover = float(explanation.review["one_way_turnover"])
    assert f"{turnover:.2%}" in response.text


@pytest.mark.slow
def test_day_non_review_date_has_no_review_card(client, desk_state):
    """Inverse of the above: a date that was not a review effective date renders no
    review card at all - otherwise the marker's presence on a review date wouldn't
    actually signal anything."""
    days = desk_state.days
    non_review_days = days.loc[~days["is_review"]]
    assert not non_review_days.empty, "fixture must contain at least one non-review day"
    date = non_review_days.iloc[-1]["date"].date()

    response = client.get("/day", params={"date": date.isoformat()})

    assert response.status_code == 200
    assert 'data-testid="review-card"' not in response.text


@pytest.mark.slow
def test_day_event_date_shows_emphasised_columns_and_apply_order(client, desk_state):
    """A day with a corporate-action divisor event renders the emphasised
    continuity-error/realised-return columns and each event's apply order."""
    from miniftse.desk.services import explain_day

    audit = desk_state.divisor_audit
    corp_events = audit.loc[audit["event_type"] != "REVIEW"]
    assert not corp_events.empty, "fixture must contain at least one corporate action"
    date = corp_events.iloc[0]["date"].date()
    explanation = explain_day(desk_state, date)
    assert explanation.events

    response = client.get("/day", params={"date": date.isoformat()})

    assert response.status_code == 200
    body = response.text
    assert 'class="metric-emphasis"' in body
    assert "continuity error" in body.lower()
    assert "realised return" in body.lower()
    for event in explanation.events:
        assert str(event["apply_order"]) in body


@pytest.mark.slow
def test_day_no_event_day_renders_market_framing(client, desk_state):
    """A plain trading day states explicitly that nothing structural happened."""
    days = desk_state.days
    plain_days = days.loc[(days["n_divisor_events"] == 0) & (~days["is_review"])]
    assert not plain_days.empty, "fixture must contain at least one plain trading day"
    date = plain_days.iloc[-1]["date"].date()

    response = client.get("/day", params={"date": date.isoformat()})

    assert response.status_code == 200
    assert "the whole move was the market" in response.text.lower()


@pytest.mark.slow
def test_day_links_memo_m2(client):
    """The 'why did the level move' memo is named somewhere on the page."""
    response = client.get("/day")
    assert "M2_why_our_index_level_moved_30bp_when_nothing_traded.md" in response.text


@pytest.mark.slow
def test_day_out_of_index_date_is_400_not_500(client):
    """A syntactically valid date the index never published a level for is a 400."""
    response = client.get("/day", params={"date": "1990-01-01"})
    assert response.status_code == 400
    assert "Traceback" not in response.text


@pytest.mark.slow
def test_day_unparseable_date_is_400_not_500(client):
    """Garbage input is a 400, never a 500 - validated before `explain_day` is called."""
    response = client.get("/day", params={"date": "not-a-date"})
    assert response.status_code == 400
    assert "Traceback" not in response.text


# --------------------------------------------------------------------------------------
# Task 6: `run_drill` service function
# --------------------------------------------------------------------------------------


def _detected_by_tuple(value: object) -> tuple[str, ...]:
    """Precomputed JSON stores `detected_by` however `run_chaos_drill` last shaped it
    for a DataFrame column (a comma-joined string, in practice) - normalise both that
    and a plain list/tuple to the same comparable shape, independent of `run_drill`'s
    own internal reshaping."""
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return tuple(value)  # type: ignore[arg-type]


@pytest.mark.slow
def test_live_drill_matches_precomputed_at_the_same_seed(desk_state):
    """If these ever disagree, one of them is lying about what the rules catch."""
    from miniftse.desk.services import run_drill

    seed = desk_state.chaos_precomputed["seed"]
    for row in desk_state.chaos_precomputed["drill"]:
        out = run_drill(desk_state, row["fault_id"], seed)
        assert out.detected == row["detected"]
        assert sorted(out.detected_by) == sorted(_detected_by_tuple(row["detected_by"]))


@pytest.mark.slow
def test_run_drill_unknown_fault_raises_key_error(desk_state):
    """The route layer turns this into a 400 - see `explain_day`'s analogous rule for
    an out-of-range date."""
    from miniftse.desk.services import run_drill

    with pytest.raises(KeyError):
        run_drill(desk_state, "F99-does-not-exist", seed=1)


@pytest.mark.slow
def test_run_drill_does_not_mutate_the_shared_baseline(desk_state):
    """`state.chaos_baseline` is the app's shared, load-once baseline. A live drill
    must never leave a mark on it - every subsequent request, and the agreement test
    above, would otherwise see a corrupted baseline.

    Runs every precomputed fault (the same battery the agreement test exercises, which
    between them mutate prices, prior_prices, shares, corp_actions/divisor_audit,
    weights and fx) against the *same* baseline object, then checks every frame field
    is bit-for-bit what it was before any of them ran.
    """
    from miniftse.desk.services import run_drill

    baseline = desk_state.chaos_baseline
    frame_fields = ("prices", "prior_prices", "shares", "corp_actions", "weights",
                    "fx", "prior_fx", "divisor_audit")
    before = {
        name: getattr(baseline, name).copy()
        for name in frame_fields if getattr(baseline, name) is not None
    }
    assert before, "fixture baseline must carry at least one frame for this test to mean anything"

    seed = desk_state.chaos_precomputed["seed"]
    for row in desk_state.chaos_precomputed["drill"]:
        run_drill(desk_state, row["fault_id"], seed)

    for name, snapshot in before.items():
        current = getattr(baseline, name)
        assert current is not None
        if isinstance(snapshot, pd.Series):
            pd.testing.assert_series_equal(current, snapshot)
        else:
            pd.testing.assert_frame_equal(current, snapshot)

    # The strongest possible proof: the same fault at the same seed, run twice, gives
    # an identical outcome - only possible if the second run saw the same baseline the
    # first one did.
    fault_id = desk_state.chaos_precomputed["drill"][0]["fault_id"]
    first = run_drill(desk_state, fault_id, seed)
    second = run_drill(desk_state, fault_id, seed)
    assert first == second


# --------------------------------------------------------------------------------------
# Task 7: Screen 2 - `/chaos` and `/chaos/run` routes
# --------------------------------------------------------------------------------------


@pytest.mark.slow
def test_chaos_get_lists_all_twelve_fault_names(client):
    """The GET renders the precomputed drill table - every one of the 12 faults, by
    name, must appear on the page."""
    from miniftse.quality.faults import FAULTS

    response = client.get("/chaos")

    assert response.status_code == 200
    assert len(FAULTS) == 12
    body = response.text
    for fault in FAULTS:
        assert fault.name in body


@pytest.mark.slow
def test_chaos_seed_input_default_value_is_within_its_own_bounds(client):
    """Regression test: the seed input was pre-filled with the precomputed battery's
    DRILL_SEED (20260809), which is outside the field's own `min`/`max` (0..999999).
    A number input whose default value fails its own HTML5 range constraint means a
    browser (and HTMX, which honours native validation) refuses to submit the form at
    all when a visitor clicks "Run live drill" without first touching the field -
    silently killing the screen's primary flow. Parses the actual rendered attributes,
    not a substring, since that is exactly the class of regression this guards
    against - a `str(seed)` appearing in the page text would not have caught it.
    """
    import html.parser

    class _SeedInputFinder(html.parser.HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.attrs: dict[str, str | None] | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attr_dict = dict(attrs)
            if tag == "input" and attr_dict.get("id") == "seed-input":
                self.attrs = attr_dict

        def handle_startendtag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            self.handle_starttag(tag, attrs)

    response = client.get("/chaos")
    assert response.status_code == 200

    parser = _SeedInputFinder()
    parser.feed(response.text)
    assert parser.attrs is not None, 'no <input id="seed-input"> found on /chaos'

    value = int(parser.attrs["value"])  # type: ignore[arg-type]
    minimum = int(parser.attrs["min"])  # type: ignore[arg-type]
    maximum = int(parser.attrs["max"])  # type: ignore[arg-type]
    assert minimum <= value <= maximum


@pytest.mark.slow
def test_chaos_get_shows_wrong_rule_finding_when_precomputed_disagrees(client, desk_state):
    """Whichever precomputed rows were caught by a rule other than their
    `expected_detector` must be visibly labelled a finding, not a pass - that column
    pair is the reason the screen exists."""
    rows = desk_state.chaos_precomputed["drill"]
    gap_rows = [r for r in rows if r["detected"] and not r["caught_by_expected"]]
    if not gap_rows:
        pytest.skip("fixture's precomputed drill has no expected != actual row")

    response = client.get("/chaos")

    assert response.status_code == 200
    assert "caught by the wrong rule" in response.text.lower()


@pytest.mark.slow
def test_chaos_get_marks_wrong_rule_row_deterministically(desk_data_dir, monkeypatch):
    """Exercises the finding styling directly with a synthetic row (detected, but not
    by `expected_detector`), independent of whether the fixture's real precomputed
    drill happens to contain such a case."""
    from fastapi.testclient import TestClient

    from miniftse.desk import app as app_module

    synthetic_row = {
        "fault_id": "F01", "fault_name": "price off by 10x", "detected": True,
        "detected_by": "ohlc_consistency", "expected_detector": "price_outliers",
        "caught_by_expected": False, "highest_severity": "BLOCK",
        "blocked_publication": True, "detail": "synthetic row for this test",
        "realism": "n/a",
    }
    monkeypatch.setattr(app_module, "chaos_drill_rows", lambda desk: [synthetic_row])

    with TestClient(app_module.create_app(data_dir=desk_data_dir)) as c:
        response = c.get("/chaos")

    assert response.status_code == 200
    assert "caught by the wrong rule" in response.text.lower()


@pytest.mark.slow
def test_chaos_run_valid_returns_html_fragment(client, desk_state):
    """A valid POST returns 200 and a bare fragment - not a full page (no nav/banner
    chrome), since it is meant to be swapped into the `/chaos` page by HTMX."""
    fault_id = desk_state.chaos_precomputed["drill"][0]["fault_id"]

    response = client.post("/chaos/run", data={"fault_id": fault_id, "seed": "1"})

    assert response.status_code == 200
    assert fault_id in response.text
    assert "site-header" not in response.text
    assert "<!doctype" not in response.text.lower()


@pytest.mark.slow
def test_chaos_run_unknown_fault_id_is_400(client):
    response = client.post("/chaos/run", data={"fault_id": "F99-nonsense", "seed": "1"})

    assert response.status_code == 400
    assert "Traceback" not in response.text


@pytest.mark.slow
def test_chaos_run_negative_seed_is_400(client, desk_state):
    fault_id = desk_state.chaos_precomputed["drill"][0]["fault_id"]

    response = client.post("/chaos/run", data={"fault_id": fault_id, "seed": "-1"})

    assert response.status_code == 400


@pytest.mark.slow
def test_chaos_run_seed_above_range_is_400(client, desk_state):
    fault_id = desk_state.chaos_precomputed["drill"][0]["fault_id"]

    response = client.post("/chaos/run", data={"fault_id": fault_id, "seed": "1000000"})

    assert response.status_code == 400


@pytest.mark.slow
def test_chaos_run_non_integer_seed_is_400_not_422(client, desk_state):
    """The plan requires 400 for every bad input on this route - FastAPI's default 422
    for a non-int form field is not acceptable, so the route must validate the raw
    string itself rather than declaring `seed: int = Form(...)`."""
    fault_id = desk_state.chaos_precomputed["drill"][0]["fault_id"]

    response = client.post("/chaos/run", data={"fault_id": fault_id, "seed": "abc"})

    assert response.status_code == 400


@pytest.mark.slow
def test_chaos_run_marks_wrong_rule_deterministically(desk_data_dir, monkeypatch):
    """Exercises the fragment's finding styling directly with a synthetic
    `DrillOutcome` (detected, but not by `expected_detector`), independent of whether
    any real fault in the fixture happens to be caught by the wrong rule."""
    from fastapi.testclient import TestClient

    from miniftse.desk import app as app_module
    from miniftse.desk.services import DrillOutcome

    synthetic_outcome = DrillOutcome(
        fault_id="F01", fault_name="price off by 10x", detected=True,
        detected_by=("ohlc_consistency",), expected_detector="price_outliers",
        severity="BLOCK", publication_blocked=True, realism="n/a",
        detail="synthetic outcome for this test", coverage_gap=None,
    )
    monkeypatch.setattr(
        app_module, "run_drill", lambda state, fault_id, seed: synthetic_outcome
    )

    with TestClient(app_module.create_app(data_dir=desk_data_dir)) as c:
        response = c.post("/chaos/run", data={"fault_id": "F01", "seed": "1"})

    assert response.status_code == 200
    assert "caught by the wrong rule" in response.text.lower()


@pytest.mark.slow
def test_chaos_run_shows_wrong_rule_finding_for_a_live_mismatch(client, desk_state):
    """If any fault's live outcome at the precomputed seed was caught by a rule other
    than its `expected_detector`, the fragment must carry the same visible finding
    label the table does."""
    from miniftse.desk.services import run_drill

    seed = desk_state.chaos_precomputed["seed"]
    mismatch = None
    for row in desk_state.chaos_precomputed["drill"]:
        outcome = run_drill(desk_state, row["fault_id"], seed)
        if outcome.detected and outcome.expected_detector not in outcome.detected_by:
            mismatch = outcome
            break
    if mismatch is None:
        pytest.skip("no fault in this fixture is caught by the wrong rule at this seed")

    response = client.post(
        "/chaos/run", data={"fault_id": mismatch.fault_id, "seed": str(seed)}
    )

    assert response.status_code == 200
    assert "caught by the wrong rule" in response.text.lower()


@pytest.mark.slow
def test_assistant_answers_an_in_scope_question(client):
    """An in-scope methodology question retrieves passages and answers with citations,
    exercising the real assistant `load_desk_state` built from the real ground_rules/
    and memos/ corpus - not a mock."""
    from miniftse.desk.services import ask

    answer = ask(client.app.state.desk, "How is free float applied to index weights?")

    assert not answer.abstained
    assert answer.citations


@pytest.mark.slow
def test_assistant_abstains_on_an_out_of_scope_question(client):
    """Abstention is a feature of this system, so it is tested as one. This is the
    exact question `rag.py`'s `MethodologyAssistant.ask` documents (lines 276-292) as
    the reason the scope check runs *before* retrieval: "What is the index level
    today?" shares all its vocabulary with the corpus and would otherwise retrieve a
    high-scoring, confidently wrong answer."""
    from miniftse.desk.services import ask

    answer = ask(client.app.state.desk, "What is the index level today?")

    assert answer.abstained
    assert answer.confidence == 0.0
    assert answer.answer


def test_select_llm_unrecognised_value_falls_back_to_offline(monkeypatch):
    """`MINIFTSE_LLM` set to garbage must resolve to `OfflineLlm`, never raise - the
    same rule the module docstring states for an unset or misspelled value. Exercised
    directly against `_select_llm` rather than a full snapshot rebuild, since this is
    the cheapest place the fallback is observable."""
    from miniftse.agents.llm import OfflineLlm
    from miniftse.desk.state import _select_llm

    monkeypatch.setenv("MINIFTSE_LLM", "garbage-value")

    assert isinstance(_select_llm(), OfflineLlm)


@pytest.mark.slow
def test_chaos_run_timeout_falls_back_to_precomputed_with_badge(
    desk_data_dir, desk_state, monkeypatch
):
    """A live drill that blows its time budget must still answer with a 200: the
    precomputed row for the same fault, with a visible "showing precomputed result"
    badge - never a hang and never an error page."""
    import time

    from fastapi.testclient import TestClient

    from miniftse.desk import app as app_module

    real_run_drill = app_module.run_drill

    def _slow_run_drill(state, fault_id, seed):
        time.sleep(0.2)
        return real_run_drill(state, fault_id, seed)

    monkeypatch.setattr(app_module, "run_drill", _slow_run_drill)
    monkeypatch.setattr(app_module, "_DRILL_TIMEOUT_SECONDS", 0.01)

    fault_id = desk_state.chaos_precomputed["drill"][0]["fault_id"]
    with TestClient(app_module.create_app(data_dir=desk_data_dir)) as c:
        response = c.post("/chaos/run", data={"fault_id": fault_id, "seed": "1"})

    assert response.status_code == 200
    assert "showing precomputed result" in response.text.lower()
    assert fault_id in response.text


# --------------------------------------------------------------------------------------
# Task 9: Screen 3a - `/ask` and `/ask/query`
# --------------------------------------------------------------------------------------

#: The exact out-of-scope question `rag.py`'s scope-violation regex (and
#: `test_assistant_abstains_on_an_out_of_scope_question` above) already relies on -
#: seeded on `/ask` deliberately so a visitor sees a refusal without thinking of one.
_OUT_OF_SCOPE_QUESTION = "What is the index level today?"


@pytest.mark.slow
def test_ask_get_shows_three_example_questions_including_out_of_scope(client):
    """The GET renders a question form and three seeded example questions, one of
    which is out of scope - a visitor sees a refusal without having to think of one."""
    response = client.get("/ask")

    assert response.status_code == 200
    body = response.text
    assert 'hx-post="/ask/query"' in body
    assert _OUT_OF_SCOPE_QUESTION in body


@pytest.mark.slow
def test_ask_query_normal_question_returns_fragment_with_citation(client):
    response = client.post(
        "/ask/query", data={"question": "How is free float applied to index weights?"}
    )

    assert response.status_code == 200
    body = response.text
    assert "site-header" not in body
    assert "<!doctype" not in body.lower()
    assert "§" in body or "SUPERSEDED" in body  # a citation, per Chunk.citation


@pytest.mark.slow
def test_ask_query_out_of_scope_question_renders_abstention_card(client):
    """Abstention is a first-class outcome, styled as its own card - not an error."""
    response = client.post("/ask/query", data={"question": _OUT_OF_SCOPE_QUESTION})

    assert response.status_code == 200
    body = response.text
    assert 'data-testid="abstention-card"' in body
    assert "cannot answer" in body.lower()


@pytest.mark.slow
def test_ask_query_over_500_chars_is_400_not_422(client):
    response = client.post("/ask/query", data={"question": "a" * 600})

    assert response.status_code == 400
    assert "Traceback" not in response.text


@pytest.mark.slow
def test_ask_query_whitespace_only_is_400(client):
    response = client.post("/ask/query", data={"question": "   "})

    assert response.status_code == 400


# --------------------------------------------------------------------------------------
# Task 10: Screen 3b/3c - `/evals` and `/draft`
# --------------------------------------------------------------------------------------


@pytest.mark.slow
def test_evals_get_shows_all_four_headline_metrics_and_failures_heading(client):
    """The four headline metrics from the design spec's Screen 3 section, plus a
    visible "Failures" heading - the minimum bar before anything else about the
    screen's content is worth testing."""
    response = client.get("/evals")

    assert response.status_code == 200
    body = response.text
    for label in ("Accuracy", "Citation precision", "Abstention accuracy",
                  "Hallucination rate"):
        assert label in body
    assert "failures" in body.lower()


@pytest.mark.slow
def test_evals_get_lists_every_failing_case_with_its_explanation(client, desk_state):
    """Every failing case from `state.evals["failures"]` (`CaseResult.explain()`,
    precomputed by `desk/snapshot.py`) must appear by name with its real explanation -
    not summarised, not truncated, not hidden behind a toggle. If the fixture's eval
    run happens to have no failures, the "no failing cases" fallback text must show
    instead, so the test still asserts something meaningful either way."""
    failures = desk_state.evals["failures"]

    response = client.get("/evals")
    assert response.status_code == 200
    body = response.text

    if not failures:
        assert "no failing cases" in body.lower()
        return
    from markupsafe import escape

    for failure in failures:
        assert failure["case_id"] in body
        # Jinja2Templates autoescapes by default, so a raw explanation containing a
        # quote (e.g. "missing required content: ['14']") is not a literal substring
        # of the rendered HTML - compare against the same escaped form the template
        # itself would have produced.
        assert str(escape(failure["explanation"])) in body


@pytest.mark.slow
def test_evals_get_does_not_collapse_the_failures_section(client):
    """Showing failures is the point of the screen (design spec: "a scoreboard that
    only shows the wins is marketing") - the failures section must not be a
    <details>/<summary> fold the way /chaos's per-row "why this fault" note is."""
    response = client.get("/evals")

    assert response.status_code == 200
    body = response.text
    section_start = body.lower().index('data-testid="failures-section"')
    section = body[section_start:section_start + 2000]
    assert "<details" not in section.lower()


@pytest.mark.slow
def test_evals_get_shows_corpus_caveat_near_the_headline_metrics(client, desk_state):
    """The IMPORTANT ledger note: the deployed evals.json is scored over the full
    corpus (ground_rules + memos), not a ground-rules-only assistant, and citation
    precision is honestly lower for it. The caveat must name the actual corpus and sit
    on the page, not be buried or omitted."""
    corpus = desk_state.evals["corpus"]
    assert corpus, "fixture's evals.json must record a non-empty corpus"

    response = client.get("/evals")

    assert response.status_code == 200
    body = response.text
    assert 'data-testid="corpus-caveat"' in body
    for name in corpus:
        assert name in body


@pytest.mark.slow
def test_evals_get_by_category_table_matches_state(client, desk_state):
    """The by-category breakdown table renders every category `state.evals` computed,
    not a subset."""
    response = client.get("/evals")

    assert response.status_code == 200
    body = response.text
    for row in desk_state.evals["by_category"]:
        assert row["category"].replace("_", " ") in body


@pytest.mark.slow
def test_draft_get_shows_the_question_picker(client):
    """3-4 canned client questions, hard-coded in `services.DRAFT_QUESTIONS`, each
    rendered as its own option in the picker."""
    from miniftse.desk.services import draft_questions

    questions = draft_questions()
    assert 3 <= len(questions) <= 4

    response = client.get("/draft")

    assert response.status_code == 200
    body = response.text
    for q in questions:
        assert q.text in body


@pytest.mark.slow
def test_draft_get_shows_fact_pack_draft_and_guard_for_the_default_question(client):
    """GET /draft renders real content for the default question - not an empty
    placeholder - the fact pack table, the drafted response, and the guard result."""
    response = client.get("/draft")

    assert response.status_code == 200
    body = response.text
    assert 'data-testid="fact-pack-table"' in body
    assert 'data-testid="draft-text"' in body
    assert 'data-testid="guard-result"' in body
    # The default question is drafted with the toggle off - the guard must show a real
    # pass, since nothing was injected to make it fail.
    assert 'data-guard-passed="true"' in body


@pytest.mark.slow
def test_draft_get_fact_pack_shows_every_fact_with_its_source(client):
    """Every fact in the default question's `FactPack` appears with its formatted
    value, description and source - the same fields `Fact.line()` carries - not just a
    subset."""
    from miniftse.desk.services import draft_questions, render_draft

    response = client.get("/draft")
    assert response.status_code == 200
    body = response.text

    # Recompute the same default outcome the route rendered, from the same desk state,
    # to check the fact table against real values rather than a hand-picked subset.
    desk = client.app.state.desk
    outcome = render_draft(desk, draft_questions()[0].question_id, inject_bad_number=False)
    for fact in outcome.response.fact_pack.facts.values():
        assert fact.formatted in body
        assert fact.description in body
        assert fact.source in body


@pytest.mark.slow
def test_draft_render_inject_false_guard_passes(client):
    response = client.post(
        "/draft/render", data={"question_id": "0", "inject_bad_number": "false"}
    )

    assert response.status_code == 200
    body = response.text
    assert 'data-guard-passed="true"' in body
    assert "injected-sentence" not in body
    assert "site-header" not in body
    assert "<!doctype" not in body.lower()


@pytest.mark.slow
def test_draft_render_inject_true_guard_does_not_pass(client):
    """The concrete demonstration the whole screen exists for: appending a fabricated
    figure to the draft makes `NumberGuard` reject it - the guard's own verification,
    not desk-side fakery (see `services._fabricate_unverified_sentence`)."""
    response = client.post(
        "/draft/render", data={"question_id": "0", "inject_bad_number": "true"}
    )

    assert response.status_code == 200
    body = response.text
    assert 'data-guard-passed="false"' in body
    assert 'data-testid="injected-sentence"' in body
    assert "BLOCKED" in body


@pytest.mark.slow
def test_draft_render_injected_number_is_absent_from_the_fact_pack(client, desk_state):
    """Confirms the injected figure genuinely does not trace to any fact - the guard's
    rejection is a real finding, not a coincidence of a number that happened to already
    be allowed."""
    from miniftse.desk.services import draft_questions, render_draft

    outcome = render_draft(desk_state, draft_questions()[0].question_id, inject_bad_number=True)

    assert outcome.injected_sentence is not None
    assert not outcome.response.guard.passed
    assert outcome.response.guard.unverified_numbers
    # Every unverified number the guard found must come from the injected sentence,
    # not from the base draft - the base draft alone passed before injection.
    for number in outcome.response.guard.unverified_numbers:
        assert number in outcome.injected_sentence


@pytest.mark.slow
def test_draft_render_out_of_range_question_id_is_400(client):
    response = client.post(
        "/draft/render", data={"question_id": "999", "inject_bad_number": "false"}
    )

    assert response.status_code == 400
    assert "Traceback" not in response.text


@pytest.mark.slow
def test_draft_render_negative_question_id_is_400(client):
    response = client.post(
        "/draft/render", data={"question_id": "-1", "inject_bad_number": "false"}
    )

    assert response.status_code == 400


@pytest.mark.slow
def test_draft_render_non_integer_question_id_is_400_not_422(client):
    """The plan requires 400 for every bad input on this route - FastAPI's default 422
    for a non-int form field is not acceptable, mirroring `/chaos/run`'s `seed`."""
    response = client.post(
        "/draft/render", data={"question_id": "abc", "inject_bad_number": "false"}
    )

    assert response.status_code == 400


def test_render_draft_unknown_question_id_raises_index_error(desk_state):
    """The route layer turns this into a 400 - see `explain_day`'s analogous rule for
    an out-of-range date."""
    from miniftse.desk.services import render_draft

    with pytest.raises(IndexError):
        render_draft(desk_state, 999, inject_bad_number=False)


def test_draft_questions_is_a_closed_set_of_three_to_four(desk_state):
    from miniftse.desk.services import draft_questions

    questions = draft_questions()
    assert 3 <= len(questions) <= 4
    assert [q.question_id for q in questions] == list(range(len(questions)))


def test_render_draft_base_draft_passes_the_guard_before_any_injection(desk_state):
    """Every canned question's un-injected draft must pass `NumberGuard` on its own -
    otherwise the inject_bad_number=False path would misleadingly show a "blocked"
    verdict on a screen built to demonstrate the guard working correctly by default."""
    from miniftse.desk.services import draft_questions, render_draft

    for question in draft_questions():
        outcome = render_draft(desk_state, question.question_id, inject_bad_number=False)
        assert outcome.response.guard.passed, (
            f"question {question.question_id} ({question.text!r}) failed the guard "
            f"with no injection: {outcome.response.guard.message}"
        )
        assert outcome.injected_sentence is None


# --------------------------------------------------------------------------------------
# Task 11: Screen 4 - `/reproducibility`
# --------------------------------------------------------------------------------------


def _unpinned_snapshot(source, destination):
    """A copy of a real snapshot whose `golden_diff.json` is the unpinned state.

    `snapshot._golden_payload` writes `{"pinned": false}` when `tests/golden/` holds no
    master, and a repository can legitimately be in that state (a fresh clone before
    `miniftse pin-golden` has run - the same case `TestGoldenMaster`'s reference test
    skips on). Rebuilding a whole snapshot against an emptied golden directory would
    cost a full index build per test, so the one file that differs is rewritten instead.
    """
    import json
    import shutil

    shutil.copytree(source, destination, dirs_exist_ok=True)
    (destination / "golden_diff.json").write_text(
        json.dumps({"pinned": False}), encoding="utf-8"
    )
    return destination


@pytest.mark.slow
def test_reproducibility_get_shows_the_comparison_panel(client, desk_state):
    """The pinned master's provenance and the comparison against this build, both from
    `state.golden_diff` - the payload `snapshot._golden_payload` already computed."""
    golden = desk_state.golden_diff
    assert golden["pinned"], "fixture snapshot must have a pinned master"

    response = client.get("/reproducibility")

    assert response.status_code == 200
    body = response.text
    assert 'data-testid="comparison-panel"' in body
    # Master provenance: which commit pinned it, when, and the content hash.
    assert golden["master"]["git_sha"][:10] in body
    assert golden["master"]["content_hash"] in body
    assert str(golden["master"]["tolerance_bps"]) in body
    # The comparison itself.
    assert str(golden["comparison"]["n_compared"]) in body
    assert 'data-testid="max-diff-bps"' in body
    assert 'data-testid="first-divergence"' in body


@pytest.mark.slow
def test_reproducibility_shows_max_diff_and_column_diffs(client, desk_state):
    """`max_diff_bps`, `mean_diff_bps` and every column in `column_diffs` are rendered,
    not summarised away - the per-column breakdown is what tells an investigator which
    quantity moved."""
    comparison = desk_state.golden_diff["comparison"]

    response = client.get("/reproducibility")

    assert response.status_code == 200
    body = response.text
    assert f"{comparison['max_diff_bps']:.4f}" in body
    assert f"{comparison['mean_diff_bps']:.4f}" in body
    for column in comparison["column_diffs"]:
        assert column.replace("_", " ") in body


@pytest.mark.slow
def test_reproducibility_unpinned_master_renders_a_state_not_an_error(
    desk_data_dir, tmp_path
):
    """`{"pinned": false}` is a state the screen renders, not a 500 - see
    `snapshot._golden_payload`'s docstring. The incident section must still be there:
    it is a written record, not a function of the current comparison."""
    from fastapi.testclient import TestClient

    from miniftse.desk.app import create_app

    data = _unpinned_snapshot(desk_data_dir, tmp_path / "unpinned")

    with TestClient(create_app(data_dir=data)) as c:
        response = c.get("/reproducibility")

    assert response.status_code == 200
    body = response.text
    assert 'data-testid="no-master-pinned"' in body
    assert "Traceback" not in body
    assert 'data-testid="comparison-panel"' not in body
    assert 'data-testid="incident-section"' in body


@pytest.mark.slow
def test_reproducibility_incident_section_is_present_when_pinned(client):
    """The September 2024 constituent divergence, with the conclusion stated verbatim.
    This is the point of the screen (design spec, Screen 4: "deliberately a published
    defect")."""
    response = client.get("/reproducibility")

    assert response.status_code == 200
    body = response.text
    assert 'data-testid="incident-section"' in body
    assert "September 2024" in body
    assert (
        "a rule whose outcome depends on floating-point summation order is not a rule"
        in body.lower()
    )


def _incident_section(body: str) -> str:
    """Everything from the incident section's marker to the end of the page.

    The incident figures are short numeric strings - "65", "153", "154", "2311" - and
    the comparison panel above is full of digits (a content hash, a git sha, an ISO
    timestamp, a row count that is also 2311). Asserting them against the whole document
    would pass on a coincidence in the panel, so every incident assertion is scoped to
    the section that is supposed to carry them.
    """
    return body[body.index('data-testid="incident-section"'):]


def _collapse(text: str) -> str:
    """Whitespace-normalised lowercase, for asserting on rendered prose.

    A Jinja template wraps its source at 90 characters, so a phrase the page reads as
    "by construction" is "by\\n    construction" in the response body. Asserting on the
    raw text would make every prose assertion a hostage to where the template happens to
    wrap, which is a reformatting away from a false failure.
    """
    return " ".join(text.split()).lower()


@pytest.mark.slow
def test_reproducibility_incident_carries_the_agreed_numbers(client):
    """The incident's facts must match the design spec's, not a parallel set invented
    here: 154 constituents against 153 at the 2024-09-20 review, `level_continuity_bps`
    0.0, 0.7426bp on the divisor, 65 of 2311 dates.

    Sourced from `_DIVERGENCE_INCIDENT` rather than restated, so the constant and the
    rendered page cannot drift apart - if someone edits one of these numbers, they have
    to mean it.
    """
    from miniftse.desk.app import _DIVERGENCE_INCIDENT as incident

    section = _incident_section(client.get("/reproducibility").text)

    for key in (
        "constituents_pinned",
        "constituents_rebuild",
        "review_date",
        "max_divisor_divergence_bps",
        "divergent_dates",
        "total_dates",
    ):
        assert str(incident[key]) in section, (
            f"incident section must state {key} ({incident[key]})"
        )


@pytest.mark.slow
def test_reproducibility_incident_attributes_its_source(client):
    """The September 2024 figures are reproduced from the ops-desk design spec - this
    repository holds no independent record of that incident, and observing it needs two
    platforms. The re-pin figures below them were measured here and are re-derivable
    from the committed golden master. The page must not present the two with the same
    authority, so the source is named on the page.
    """
    from miniftse.desk.app import _DIVERGENCE_INCIDENT as incident

    section = _incident_section(client.get("/reproducibility").text)

    assert 'data-testid="incident-provenance"' in section
    assert incident["source"] in section


@pytest.mark.slow
def test_reproducibility_presents_the_incident_as_closed_and_historical(client):
    """Closed, and honest about which numbers are which: the incident figures are the
    pre-fix history, the comparison panel above is the current build. A page that let a
    reader think the 0.74bp divergence is live would be worse than not publishing it."""
    section = _incident_section(client.get("/reproducibility").text).lower()

    assert 'data-testid="incident-status"' in section
    assert "closed" in section
    assert "historical" in section
    # The remediation the incident claims shipped, named specifically enough to check
    # against `universe/banding.py`.
    for shipped in ("fsum", "quantis", "tie-break"):
        assert shipped in section


@pytest.mark.slow
def test_reproducibility_repin_is_not_framed_as_a_knife_edge(client):
    """`abs(1.0 - 0.98) == 0.020000000000000018` is what IEEE-754 gives on every
    platform, every run - so the pre-fix comparison failed *by construction* whenever a
    Small Cap incumbent was also the smallest name in the universe, not on a freak
    coincidence of last bits. What is rare is the configuration, not the arithmetic.

    An earlier draft of this page called it "0.6 ulp" and read as a knife-edge, which
    flattered the old code: a defect that returns the same wrong answer every time is
    worse than one that flickers, because it looks like a rule. This test exists so that
    framing cannot come back.
    """
    section = _incident_section(client.get("/reproducibility").text)
    prose = _collapse(section)

    assert 'data-testid="repin-note"' in section
    assert "ulp" not in prose, "the ulp framing was wrong; do not reintroduce it"
    assert "not a knife-edge" in prose
    # The page must say the failure was reliable, and that the rarity is the setup.
    assert "by construction" in prose
    assert "what is rare is the <em>configuration</em>" in prose
    # And it must record that the Small/Micro buffer can never release, so the page
    # does not imply a buffer that does something it structurally cannot.
    assert "never release anything" in prose


# --------------------------------------------------------------------------------------
# Task 12: Screen 5 - `/index`
# --------------------------------------------------------------------------------------


@pytest.mark.slow
def test_index_get_200_with_return_stat_constituents_header_and_six_schemes(
    client, desk_state
):
    """Brief Step 1: `GET /index` is 200 and contains the annualised-return stat, the
    constituents table header, and the six scheme names.

    Scheme names are derived from `app._SCHEME_LABELS`, not hardcoded twice: that dict
    is the closed set of display labels the Capacity section actually renders, keyed by
    exactly the six keys `capacity.json["schemes"]` carries (`weighting.schemes.
    SCHEME_PROPERTIES`, stable since it is a module-level constant the library
    publishes) - so this test and the template can never silently drift apart.
    """
    from miniftse.desk.app import _SCHEME_LABELS

    response = client.get("/index")

    assert response.status_code == 200
    body = response.text

    stats = desk_state.overview["stats"]
    assert f'{stats["annualised_return"] * 100:.1f}%' in body

    assert "Security" in body  # constituents table header
    assert "Weight" in body

    assert set(_SCHEME_LABELS) == set(desk_state.capacity["schemes"])
    for label in _SCHEME_LABELS.values():
        assert label in body


@pytest.mark.slow
def test_index_shows_all_four_section_anchors(client):
    """Four in-page sections, reached by anchor nav - not a second HTMX round trip per
    tab (the plan's "pick the simpler" instruction)."""
    response = client.get("/index")

    assert response.status_code == 200
    body = response.text
    assert 'data-testid="index-section-nav"' in body
    for anchor in ("overview", "constituents", "capacity", "risk-attribution"):
        assert f'href="#{anchor}"' in body
        assert f'id="{anchor}"' in body


@pytest.mark.slow
def test_index_references_capacity_js_and_it_is_served(client):
    """`capacity.js` is referenced from the page and is actually served underneath
    `/static` - a dangling reference would 404 silently in a browser but pass a naive
    text-only check."""
    response = client.get("/index")

    assert response.status_code == 200
    assert '/static/capacity.js' in response.text

    js_response = client.get("/static/capacity.js")
    assert js_response.status_code == 200
    assert "capacityConstrainedWeights" in js_response.text
    assert "weightedAverageDaysToTrade" in js_response.text


@pytest.mark.slow
def test_index_embeds_overview_chart_series_as_json(client, desk_state):
    """The PR/GTR/NTR history chart's data is embedded as JSON (a script tag with the
    arrays), not baked into inline Chart.js call arguments the template would have to
    hand-serialise itself."""
    import json

    response = client.get("/index")
    assert response.status_code == 200
    body = response.text

    start = body.index('id="overview-chart-data"')
    script_start = body.index(">", start) + 1
    script_end = body.index("</script>", script_start)
    payload = json.loads(body[script_start:script_end])

    overview = desk_state.overview
    assert payload["dates"] == overview["dates"]
    assert payload["pr"] == overview["pr"]
    assert payload["gtr"] == overview["gtr"]
    assert payload["ntr"] == overview["ntr"]


@pytest.mark.slow
def test_index_embeds_capacity_constituents_and_params_for_the_slider(client, desk_state):
    """The fund-size slider's client-side recompute (`static/capacity.js`) needs the
    constituent weights/adv and the capacity params in the page - embedded as JSON,
    matching exactly `state.capacity`'s own `constituents`/`capacity_params`, not a
    reshaped or partial copy."""
    import json

    response = client.get("/index")
    assert response.status_code == 200
    body = response.text

    start = body.index('id="capacity-constituents-data"')
    script_start = body.index(">", start) + 1
    script_end = body.index("</script>", script_start)
    payload = json.loads(body[script_start:script_end])

    capacity = desk_state.capacity
    assert payload["capacity_params"] == capacity["capacity_params"]
    assert len(payload["constituents"]) == len(capacity["constituents"])
    assert {c["security_id"] for c in payload["constituents"]} == {
        c["security_id"] for c in capacity["constituents"]
    }


@pytest.mark.slow
def test_index_constituents_table_lists_every_security(client, desk_state):
    """Every constituent in `state.constituents` appears in the table - not a
    truncated top-N."""
    response = client.get("/index")

    assert response.status_code == 200
    body = response.text
    for row in desk_state.constituents["constituents"]:
        assert row["security_id"] in body


@pytest.mark.slow
def test_index_stat_tiles_show_all_four_overview_stats(client, desk_state):
    """The overview tiles carry the library's own calendar-year annualised return, the
    annualised vol, the max drawdown and the divisor-event count - the exact figures
    `overview.json["stats"]` already computed, formatted the same way the template
    formats them, not a recomputation."""
    response = client.get("/index")

    assert response.status_code == 200
    body = response.text
    stats = desk_state.overview["stats"]

    assert f'{stats["annualised_return"] * 100:.1f}%' in body
    assert f'{stats["annualised_vol"] * 100:.1f}%' in body
    assert f'{stats["max_drawdown"] * 100:.1f}%' in body
    assert str(stats["divisor_events"]) in body


@pytest.mark.slow
def test_index_scheme_table_shows_every_scheme_property(client, desk_state):
    """The six-scheme trade-off table renders every property string from
    `capacity.json["schemes"]` verbatim - not a subset and not paraphrased."""
    response = client.get("/index")

    assert response.status_code == 200
    body = response.text
    from markupsafe import escape

    for props in desk_state.capacity["schemes"].values():
        for value in props.values():
            assert str(escape(value)) in body


@pytest.mark.slow
def test_index_risk_and_attribution_onepagers_render_title_and_a_table_cell(
    client, desk_state
):
    """Both parsed one-pagers (`state.risk_attribution["risk"]`/`["attribution"]`)
    render their title and at least one real table cell from their sections - proof the
    payload's `sections`/`table` shape is actually walked, not just its top-level keys."""
    response = client.get("/index")

    assert response.status_code == 200
    body = response.text
    assert 'data-testid="risk-onepager"' in body
    assert 'data-testid="attribution-onepager"' in body

    for doc in (desk_state.risk_attribution["risk"], desk_state.risk_attribution["attribution"]):
        assert doc["title"] in body
        tabled_sections = [s for s in doc["sections"] if s["table"]]
        assert tabled_sections, "fixture one-pager must carry at least one table to test against"
        first_row = tabled_sections[0]["table"]["rows"][0]
        assert first_row[0] in body


def test_scheme_labels_cover_exactly_the_library_scheme_properties():
    """`app._SCHEME_LABELS` (display names for the Capacity section) must name exactly
    the keys `weighting.schemes.SCHEME_PROPERTIES` publishes - not a stale subset or an
    invented extra scheme - since `capacity.json["schemes"]` is that dict written
    verbatim by `desk/snapshot.py`."""
    from miniftse.desk.app import _SCHEME_LABELS
    from miniftse.weighting.schemes import SCHEME_PROPERTIES

    assert set(_SCHEME_LABELS) == set(SCHEME_PROPERTIES)


# --------------------------------------------------------------------------------------
# Task 13: rate limiting and centralised input validation
# --------------------------------------------------------------------------------------


class _FakeClock:
    """A zero-argument `time.monotonic`-shaped callable a test can move by hand -
    `TokenBucketLimiter`'s injected clock. Every Task 13 test uses this instead of
    `time.sleep`: "61 rapid calls" must 429 deterministically on the 61st regardless of
    how fast the test machine is, and "refills after 60 seconds" must be provable
    without a 60-second test.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---- TokenBucketLimiter, unit level -----------------------------------------------


def test_token_bucket_allows_up_to_capacity_then_denies():
    """`rate_per_minute` calls succeed; the next one, in the same instant, does not -
    the class-level building block behind "61 rapid POSTs yield at least one 429"."""
    from miniftse.desk.limits import TokenBucketLimiter

    clock = _FakeClock()
    limiter = TokenBucketLimiter(rate_per_minute=5, clock=clock)

    results = [limiter.allow("k") for _ in range(6)]

    assert results == [True, True, True, True, True, False]


def test_token_bucket_refills_after_60_seconds():
    """An exhausted bucket denies immediately, but grants again once the fake clock has
    advanced a full 60-second window - no sleeping, just moving the injected clock."""
    from miniftse.desk.limits import TokenBucketLimiter

    clock = _FakeClock()
    limiter = TokenBucketLimiter(rate_per_minute=3, clock=clock)
    for _ in range(3):
        assert limiter.allow("k")
    assert not limiter.allow("k")

    clock.advance(60.0)

    assert limiter.allow("k")


def test_token_bucket_different_keys_do_not_share_a_bucket():
    """Exhausting one key's bucket must not affect a different key's - each IP gets its
    own budget. Exercised at the `TokenBucketLimiter` unit level rather than through two
    simulated client IPs on a `TestClient`, per the task's own suggestion: forcing a
    `TestClient` request to present a second, distinct `request.client.host` has no
    straightforward public API in this project's Starlette/FastAPI version, whereas the
    limiter's own key-independence is exactly what this test needs to prove."""
    from miniftse.desk.limits import TokenBucketLimiter

    clock = _FakeClock()
    limiter = TokenBucketLimiter(rate_per_minute=1, clock=clock)

    assert limiter.allow("1.2.3.4")
    assert not limiter.allow("1.2.3.4")  # that key's bucket is now empty
    assert limiter.allow("5.6.7.8")  # a different key, unaffected


def test_token_bucket_prunes_buckets_idle_past_the_threshold():
    """A bucket untouched for longer than `idle_after_seconds` is dropped from
    `_buckets` on the next `allow()` call for any key - proof the dict cannot grow
    unbounded across a long-lived deployment. Reads the private `_buckets` dict, the
    same whitebox style this test module already uses elsewhere (e.g.
    `monkeypatch.setattr` against module internals) - there is no public introspection
    method, and adding one only for this test would be surface area nothing else needs.
    """
    from miniftse.desk.limits import TokenBucketLimiter

    clock = _FakeClock()
    limiter = TokenBucketLimiter(rate_per_minute=5, clock=clock, idle_after_seconds=100.0)

    limiter.allow("stale")
    clock.advance(150.0)
    limiter.allow("fresh")  # any call sweeps stale entries first

    assert "stale" not in limiter._buckets
    assert "fresh" in limiter._buckets


def test_token_bucket_rejects_non_positive_rate():
    from miniftse.desk.limits import TokenBucketLimiter

    with pytest.raises(ValueError, match="positive"):
        TokenBucketLimiter(rate_per_minute=0)


# ---- Wired into the app, via `create_app`'s injectable rate-limit parameters ------


@pytest.mark.slow
def test_61_rapid_posts_from_one_client_yield_at_least_one_429(desk_data_dir):
    """Task 13 Step 1: 61 rapid `POST /ask/query` calls from one client, all in the same
    frozen instant, exercise the deployed default (60/minute) - the first 60 succeed and
    the 61st is a 429, never a 500 and never a raw traceback."""
    from fastapi.testclient import TestClient

    from miniftse.desk.app import create_app

    clock = _FakeClock()
    app = create_app(data_dir=desk_data_dir, rate_limit_clock=clock)
    with TestClient(app) as c:
        responses = [
            c.post(
                "/ask/query",
                data={"question": "How is free float applied to index weights?"},
            )
            for _ in range(61)
        ]

    statuses = [r.status_code for r in responses]
    assert statuses[:60] == [200] * 60
    assert statuses[60] == 429
    assert "Traceback" not in responses[60].text


@pytest.mark.slow
def test_a_get_is_never_rate_limited(desk_data_dir):
    """70 rapid `GET /day` calls - more than the 60/minute POST budget - all come back
    200: rate limiting is wired onto the three POST routes only."""
    from fastapi.testclient import TestClient

    from miniftse.desk.app import create_app

    clock = _FakeClock()
    app = create_app(data_dir=desk_data_dir, rate_limit_clock=clock)
    with TestClient(app) as c:
        statuses = [c.get("/day").status_code for _ in range(70)]

    assert statuses == [200] * 70


@pytest.mark.slow
def test_the_three_post_routes_share_one_limiter_budget(desk_data_dir, desk_state):
    """`/chaos/run`, `/ask/query` and `/draft/render` draw from the same per-IP bucket,
    not one 60/minute budget each: 40 `/chaos/run` posts followed by 21 `/ask/query`
    posts from one client (61 total) must 429 on the 61st, proving the third route's
    request counts against the budget the first route already spent down."""
    from fastapi.testclient import TestClient

    from miniftse.desk.app import create_app

    fault_id = desk_state.chaos_precomputed["drill"][0]["fault_id"]
    clock = _FakeClock()
    app = create_app(data_dir=desk_data_dir, rate_limit_clock=clock)
    with TestClient(app) as c:
        chaos_statuses = [
            c.post("/chaos/run", data={"fault_id": fault_id, "seed": "1"}).status_code
            for _ in range(40)
        ]
        ask_statuses = [
            c.post(
                "/ask/query",
                data={"question": "How is free float applied to index weights?"},
            ).status_code
            for _ in range(21)
        ]

    assert chaos_statuses == [200] * 40
    assert ask_statuses[:20] == [200] * 20
    assert ask_statuses[20] == 429


@pytest.mark.slow
def test_rate_limit_refills_across_app_after_60_fake_seconds(desk_data_dir):
    """The same 429-then-refill behaviour `TokenBucketLimiter` proves in isolation,
    wired all the way through the app: exhaust the budget, advance the injected clock by
    60 seconds, and the next request succeeds again."""
    from fastapi.testclient import TestClient

    from miniftse.desk.app import create_app

    clock = _FakeClock()
    app = create_app(data_dir=desk_data_dir, rate_limit_per_minute=2, rate_limit_clock=clock)
    with TestClient(app) as c:
        first = c.post("/ask/query", data={"question": "q1"})
        second = c.post("/ask/query", data={"question": "q2"})
        third = c.post("/ask/query", data={"question": "q3"})

        clock.advance(60.0)
        fourth = c.post("/ask/query", data={"question": "q4"})

    assert (first.status_code, second.status_code, third.status_code) == (200, 200, 429)
    assert fourth.status_code == 200


def test_create_app_default_rate_limit_matches_the_deployed_constant():
    """`create_app`'s `rate_limit_per_minute` default must be the real deployed budget,
    not a value a test happened to leave behind - guards against a future edit changing
    one without the other."""
    import inspect

    from miniftse.desk.app import create_app
    from miniftse.desk.limits import DEFAULT_RATE_LIMIT_PER_MINUTE

    default = inspect.signature(create_app).parameters["rate_limit_per_minute"].default
    assert default == DEFAULT_RATE_LIMIT_PER_MINUTE == 60


# ---- Centralised validation: same rules, one home -----------------------------------


def test_validate_fault_id_rejects_unknown_id():
    from fastapi import HTTPException

    from miniftse.desk.limits import validate_fault_id

    with pytest.raises(HTTPException) as exc_info:
        validate_fault_id("F99-nonsense")
    assert exc_info.value.status_code == 400


def test_validate_seed_parses_and_range_checks():
    from fastapi import HTTPException

    from miniftse.desk.limits import validate_seed

    assert validate_seed("42") == 42
    for bad in ("-1", "1000000", "abc"):
        with pytest.raises(HTTPException) as exc_info:
            validate_seed(bad)
        assert exc_info.value.status_code == 400


def test_validate_question_strips_and_enforces_length_cap():
    from fastapi import HTTPException

    from miniftse.desk.limits import MAX_QUESTION_LENGTH, validate_question

    assert validate_question("  hello  ") == "hello"
    with pytest.raises(HTTPException) as exc_info:
        validate_question("   ")
    assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        validate_question("a" * (MAX_QUESTION_LENGTH + 1))
    assert exc_info.value.status_code == 400


def test_validate_question_id_rejects_non_integers():
    from fastapi import HTTPException

    from miniftse.desk.limits import validate_question_id

    assert validate_question_id("2") == 2
    with pytest.raises(HTTPException) as exc_info:
        validate_question_id("abc")
    assert exc_info.value.status_code == 400


def test_validate_date_rejects_unparseable_and_out_of_set_dates():
    from fastapi import HTTPException

    from miniftse.desk.limits import validate_date

    available = [dt.date(2020, 1, 2), dt.date(2020, 1, 3)]
    assert validate_date("2020-01-02", available) == dt.date(2020, 1, 2)
    with pytest.raises(HTTPException) as exc_info:
        validate_date("not-a-date", available)
    assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        validate_date("1990-01-01", available)
    assert exc_info.value.status_code == 400
