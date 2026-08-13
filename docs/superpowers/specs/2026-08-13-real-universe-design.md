# Real-data universe — design

**Date:** 2026-08-13 · **Status:** implemented · **Modules touched:** `data/`, `production/`, `quality/`, `cli`

## The problem

`data/providers.py` opens with a claim:

> Research code binds to these Protocols, never to a vendor. Swapping the synthetic
> generator for yfinance, or yfinance for `lseg.data`, must not require touching anything
> in `calc/`, `factors/` or `risk/`.

That claim was true of `calc/`, `factors/` and `risk/`, and false of `production/`. Six
call sites reached past the Protocol into a **private attribute** of the concrete
generator:

| Call site | What it took |
|---|---|
| `production/build.py:97-99` | `universe._generated["prices" / "shares" / "corp_actions"]` |
| `production/variants.py:176,198,208` | the same three |
| `production/daily.py:168` | `_generated["prices"]` for the session list |
| `data/store.py:295-297` | the same three |
| `quality/faults.py:460` | `_generated["prices"]` |

Type annotations bound to `SyntheticUniverse` rather than `MarketDataProvider` in
`build`, `daily`, `variants` and `store`. `variants` and `daily` also *constructed* their
own universe internally, which hard-codes the class and assumes construction is free and
deterministic — true of a generator, false of a snapshot on disk.

So a real provider could satisfy every documented method and still not be substitutable.

## Decisions

**D-1 — the on-disk snapshot is the contract.** `SyntheticUniverse.materialise()` already
wrote nine parquet tables plus a `config.json` fingerprint. That layout *was* the
interface; it just was not named as one. Promoting it costs one Protocol and one loader
rather than a new abstraction.

**D-2 — `UniverseData` extends `MarketDataProvider`, it does not replace it.**
`MarketDataProvider` says what you can *ask* (as-of queries). `UniverseData` says what you
can *hold*: the whole panel, its calendar, and an identity. The distinction is load-bearing
— `IndexCalculator.run` walks a full price panel day by day, so per-date provider queries
would be both wrong (the calculator must see delistings coming) and unusably slow.

**D-3 — synthetic stays the default.** Every existing caller passing no universe gets a
generated one, unchanged. Real data is opt-in via `BuildSpec.universe`. This preserves the
three properties `synthetic.py`'s docstring defends: clean-clone builds, golden-master hash
pinning, and deliberately-placed pathologies.

**D-4 — fetching is separated from building.** Network access lives in `data/real.py` and
produces files. Builds read files. A real build is therefore as deterministic as a
synthetic one for as long as the snapshot is kept, and re-fetching revised data yields a
*different fingerprint* rather than silently changing an answer.

**D-5 — fingerprint is content-addressed for snapshots.** A generator hashes its config; a
snapshot hashes its bytes. Both answer "which data was this built from", which is what an
audit needs.

**D-6 — defects are data, not prose.** `data.real.DEFECTS` is a dict written into every
snapshot's `config.json` under `provenance`. An analysis built on a snapshot cannot claim
not to have known.

## Architecture

```
data/real.py          RealUniverseBuilder ──writes──▶  snapshot/*.parquet
                       (SEC + Yahoo + FRED)                    │
data/synthetic.py     SyntheticUniverse ──materialise()──▶     │
                              │                                │
                              │ satisfies                      │ loads
                              ▼                                ▼
data/providers.py         UniverseData  ◀──satisfies── MaterialisedUniverse
                              │
                              ▼
production/build.py    BuildSpec(universe=...) ──▶ IndexCalculator ──▶ history
```

`RealUniverseBuilder` is deliberately **not** a `UniverseData`. It is a builder that emits
a snapshot; `MaterialisedUniverse` reads it. That keeps network code out of the build path
entirely.

## Source map

| Table | Source | Quality |
|---|---|---|
| securities / listings / identifiers | SEC `company_tickers.json` + `submissions` | current registrants only |
| shares | SEC `companyconcept` `dei:EntityCommonStockSharesOutstanding` | **genuinely point-in-time** |
| fundamentals | SEC `companyconcept` us-gaap | **genuinely point-in-time**, restatements flagged |
| prices | Yahoo via yfinance | split-adjusted, survivors only |
| corp_actions | Yahoo dividends + splits | no spin-offs, no mergers |
| classifications | SEC SIC → ICB level 1 | crude, not point-in-time |
| fx | constant USD + FRED `DGS3MO` | correct — US-only universe |

The SEC data is the good part: every fact carries a `filed` date, so the point-in-time
contract in `FundamentalProvider` is *met* rather than approximated, and a restatement
appears as a second row rather than overwriting the first.

### Rejected: EDGAR Financial Statement Data Sets

The quarterly `sub.txt` in the FSDS ZIPs is a genuinely survivorship-free, point-in-time
list of *who was filing* — a company that filed in 2018Q1 and stopped in 2022Q4 is visible
in the data. That would have solved survivorship at the issuer level.

Measured: each quarterly ZIP is **92 MB**, so ten years is ~3.7 GB. Deferred, not
dismissed — this is the single highest-value upgrade to the snapshot, and the loader seam
does not change when it lands.

## Known defects

Three change index numbers and are not fixable from free sources:

1. **Survivorship.** The universe is current registrants, so acquired and delisted names
   are absent. Returns are measured on survivors and biased upward. Largest distortion.
2. **No free float.** No free source publishes it; every security is set to `1.0`, which
   degenerates float-cap weighting to full-cap weighting and means the free-float
   eligibility screens can never bind.
3. **Split-adjusted prices.** Yahoo has no as-traded series, so historical market
   capitalisation cannot be reconstructed from price × shares across a split.

Plus: no spin-offs or mergers in the actions series; candidate ranking is by *current*
market cap (look-ahead in the pool, though not in the weights — membership is still decided
by the reconstitution rules at each review); SIC is not point-in-time.

## Empirical finding: the universe has a floor

A 12-name smoke build failed in `weighting/capping.py`:

```
CappingError: infeasible: 5 names capped at 0.1000 can hold at most 0.5000 of the index
```

This is correct behaviour, and it quantifies the "small universe" trade-off: UCITS 5/10/40
needs enough eligible names to be satisfiable at all. It is the reason the clean tier is
~200 rather than ~60 — below that, eligibility screens go vacuous, the covariance matrix
is barely estimable, and the TE-constrained optimiser stops being a real constraint.

## Incident: a partial fetch is more dangerous than a failed one

The first snapshot built cleanly and produced a gate-passing index. A later re-run was
rate-limited by Yahoo — which reports throttling as `possibly delisted; no price data
found`, for AAPL and MSFT as readily as for a genuine delisting. One ticker of 200
survived, and two defects turned that into data loss:

- `prices_and_actions` raised only when the result was *completely* empty, so one survivor
  was enough to proceed.
- `build()` wrote parquet straight into the destination, overwriting a known-good snapshot.

Both are fixed (D-019): an 80% coverage floor, staging-plus-rename, and per-ticker price
caching so a throttled run resumes rather than restarts. `tests/test_real_universe.py`
pins both guards.

The general lesson is worth keeping separate from the specific bug. For this pipeline a
*partial* result is more dangerous than a failure, because a snapshot covering a fraction
of its universe still builds an index history that looks entirely plausible — which is the
same argument the publication gate rests on, applied one layer earlier.

## Verification

- `ruff` clean.
- `mypy --strict`: 18 pre-existing `unused-ignore` errors in 9 files, **identical at
  pristine HEAD** (confirmed by stashing). Environment drift from newer pandas-stubs, not
  introduced here.
- Full suite: **217 tests, 2 skipped, exit 0** — the synthetic path and golden master are
  unaffected.

## Follow-on work

- EDGAR FSDS `sub.txt` for a survivorship-free issuer universe (see above).
- The raw tier (~800 names) and the two-tier comparison: build the same index rigorously
  and best-effort, and report the difference in bps/yr. That is the M1 thesis at index
  level, and the ETL already takes `n_securities`.
- Hand-resolve delisted price history for the clean tier's dead names.
- A real-snapshot golden master: the snapshot is content-hashed, so pinning a frozen real
  build is now possible.
