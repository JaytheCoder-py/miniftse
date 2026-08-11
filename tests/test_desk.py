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
