# miniftse-viz: a static visualization app

**Date:** 2026-08-11
**Status:** approved, pending implementation plan

## Purpose

miniftse currently produces its outputs as files: parquet levels/weights, markdown
one-pagers, JSON manifests. There is no way to point a hiring manager or a curious
visitor at a link and have them see the index, or explore what "index capacity" means,
without cloning the repo and reading files. This adds a small static web app, `viz/`,
that visualizes what the project builds and makes the factor-exposure / turnover /
capacity trade-off interactive.

Non-goals: no live recomputation of the index itself, no backend, no authentication, no
mobile-app packaging. This is a read-only window onto artefacts the CLI already
produces (plus one small field added to an existing one — see below), not a new way to
run miniftse.

## Audience & hosting

Public, unauthenticated, hosted on GitHub Pages from this repo at `/viz`. Visitors are
assumed hostile-adjacent only in the "might send weird input" sense (there is no input
to send — it's a static site) — not a threat model that needs real hardening.

## Architecture

```
src/miniftse/calc/state.py    + adv field on Constituent
src/miniftse/production/daily.py  + persist adv through IndexStateFile (see below)

viz/
  export.py               reads artefacts/ -> writes viz/data/*.json
  index.html
  style.css
  app.js                  tab navigation, fetch(), Chart.js rendering
  capacity.js             ported capacity-trim algorithm, drives the fund-size slider
  data/
    overview.json
    capacity.json
    constituents.json
    risk_attribution.json

.github/workflows/pages.yml   deploy viz/ to GitHub Pages on push to main
Makefile                       + `viz` target running export.py
```

No backend, no npm, no build step for the frontend. `export.py` is a one-off local (or
CI, if later automated) step; the deployed artifact is static files plus static JSON.

## Data pipeline

### Existing artefacts cover most of this already

Checked directly rather than assumed: `artefacts/state/{index_id}_state.json` (written
by `daily`/`seed-state`, already exists in this working tree) persists each
constituent's `price, shares, free_float_factor, capping_factor, fx_rate, currency,
country, icb_industry, size_band` (`production/daily.py:72-82`). `weights.parquet`
already has `date, security_id, weight`. So for the constituents/capacity views,
`export.py` can get security_id, weight, sector (`icb_industry`), country, and
size_band by joining the latest date of `weights.parquet` with `state.json` — no
production code changes needed for any of those four fields.

### The one real gap: ADV

`Constituent` (`calc/state.py`) has no ADV field, and `state.json` doesn't carry it,
so it isn't available anywhere post-build today. ADV already gets computed as part of
`SecurityInputs` (`weighting/schemes.py:35` — `adv: float`) when a weighting step
runs; it just isn't threaded one hop further into the state that gets persisted.
Regenerating it from scratch in `export.py` by re-running the synthetic universe
generator was considered and rejected: generation is RNG-driven, and reconstructing
the exact per-security ADV outside the pipeline that already computed it risks silent
drift if parameters or generator code ever change.

Instead: add an `adv` field to `Constituent` (`calc/state.py`), and persist it through
`IndexStateFile.from_state`/`to_state` in `production/daily.py`, following the exact
existing pattern used for `country`/`icb_industry`/`size_band` at `daily.py:72-93`.
Exact wiring point (where in the daily pipeline `SecurityInputs.adv` is available to
attach to the `Constituent` being built) is left to the implementation plan — this is
a same-shape addition to code that already does this for four other fields, not a new
mechanism.

This is a pure addition: existing artefacts, columns, and CLI behaviour are otherwise
unchanged. `build-index` (the full historical rebuild, which doesn't go through
`DailyJob`) does not populate `state.json` — the capacity/constituents views are
therefore sourced from the latest `daily`/`seed-state` state snapshot, not from an
arbitrary `build-index` run. That's the semantically correct source anyway: it's the
"as of now" snapshot, whereas `weights.parquet`/`levels.parquet` (used for the
Overview tab's history chart) span the full multi-year backtest.

### `viz/export.py`

Reads `weights.parquet`, `levels.parquet`, `state.json`, and the two one-pagers;
writes four JSON files under `viz/data/`. No `import miniftse` beyond reading
`weighting.schemes.SCHEME_PROPERTIES` (a static dict, safe to reuse directly) —
everything else is file I/O and pandas.

- **`overview.json`** — from `MFTSE-GLOBAL_levels.parquet`: `{dates: [...],
  pr: [...], gtr: [...], ntr: [...], stats: {annualised_return, annualised_vol,
  max_drawdown, divisor_events}}`. Stats computed in `export.py` with pandas
  (standard annualised-return/vol/drawdown formulas over the GTR series;
  divisor-event count = number of days the divisor column changes vs. the prior
  day). Review count is not included — it isn't cleanly derivable from
  `levels.parquet` alone; not worth a separate data source for one stat tile.

- **`capacity.json`** — `{schemes: <SCHEME_PROPERTIES dict, dumped as-is>,
  constituents: [{security_id, weight, adv, sector, country, size_band}, ...]}`,
  built by joining latest-date `weights.parquet` rows with `state.json`.

- **`constituents.json`** — the same joined constituent list, sorted by weight
  descending.

- **`risk_attribution.json`** — parsed from `artefacts/risk_onepager.md` and
  `artefacts/attribution_onepager.md`. Both are small, regular markdown tables; a
  ~30-line table parser in `export.py` turns each into `{headline, tables: [{title,
  columns, rows}]}`. Rejected alternative: re-running `reporting/analytics.py`'s
  `analyse_risk`/`analyse_attribution` to get structured data directly — that
  requires a full index + factor-model build in memory just to reproduce numbers
  already computed and saved to these files.

`export.py` fails fast (non-zero exit, clear message) if an expected artefact is
missing, rather than emitting partial JSON. A broken local export should never be
committed.

## Frontend

Single-page app, four tab-navigated sections: **Overview**, **Capacity**,
**Constituents**, **Risk & Attribution**. Plain HTML/CSS/JS, `fetch()` over the static
JSON files, Chart.js loaded from a CDN `<script>` tag for the line/bar charts (no
build step; GitHub Pages has no CSP restriction against a CDN script, unlike a Claude
Artifact).

- **Overview** — stat tiles (annualised return / vol / max drawdown / reviews), a
  PR/GTR/NTR level line chart over the full history.
- **Capacity** — the six-scheme trade-off table (turnover / capacity / factor
  exposure / explainability / use-when, straight from `SCHEME_PROPERTIES`), plus a
  fund-size slider (see below) showing live trimmed-weight and weighted-average
  days-to-trade for the current constituent snapshot.
- **Constituents** — sortable/filterable table: security, weight, sector, country,
  size band.
- **Risk & Attribution** — the parsed tables from the two one-pagers, rendered as a
  bar chart (risk contribution by factor) and a table (attribution by design
  decision / by sector), with the same explanatory prose the one-pagers already
  carry.

### Capacity interactivity

`capacity_constrained_weights()` (`weighting/schemes.py:184-225`) is ~20 lines of pure
math: cap each weight at `fund_size × weight / (adv × participation × max_days)`,
redistribute the residual across uncapped names, repeat to convergence. `capacity.js`
is a direct port, run against the `constituents` array from `capacity.json`. Moving
the fund-size slider recomputes trimmed weights and weighted-average days-to-trade
entirely client-side — no server round-trip, no precomputed scenario grid needed.

## Deployment

`.github/workflows/pages.yml`: on push to `main`, checkout, upload `viz/` as a Pages
artifact, deploy. `export.py` is *not* part of this workflow — it needs the full
`miniftse` Python environment and is a low-frequency step (re-run when the reference
build changes), so it runs locally via `make viz` and its output (`viz/data/*.json`)
is committed like any other generated-but-tracked file (the repo already does this for
`artefacts/`).

`make viz` target added to the Makefile, following the existing style
(`$(UV) run python viz/export.py`).

## Error handling & edge cases

Static site: a missing or malformed JSON file should fail loud in the browser (a
small "couldn't load this section" message per tab) rather than rendering a blank
page. `export.py` failing fast on a missing artefact (above) is the main real
safeguard — it stops a broken export from ever reaching `viz/data/`.

## Testing

Proportionate to scope: no new JS test suite. `export.py` gets a couple of
`pytest`-style checks (new `tests/test_viz_export.py`, or folded into an existing
integration test) asserting it produces valid JSON matching the expected keys/shape
for each of the four output files, so a future artefact-format change can't silently
break the site without a test noticing. Frontend is verified by loading the exported
page in a browser and checking each of the four sections renders and the slider
updates the numbers.

## Open items handed to the implementation plan

- Exact wiring point for attaching `SecurityInputs.adv` to the `Constituent` being
  built during a daily run, so it survives into `IndexStateFile`.
- Chart.js version/CDN URL pin.
- Exact annualised-return/vol/max-drawdown formulas to match what `factsheet.md`
  already reports, so the numbers on the Overview tab agree with the existing
  factsheet.
