# Handover

**Last session ended:** 2026-08-11 · **Branch:** `master` · **Head:** `e358448`

---

## What this repo is

Two things sharing one directory, deliberately kept apart:

| Path | Branch | Purpose |
|---|---|---|
| `miniftse/` | `master` | The **reference implementation** — a complete, working index platform. Read it, run it, break it. |
| `miniftse-training/` | `training/week-01` | **Jason's learning track.** Untouched since the tutor session. Tag `v0.0-tutor-start`. |

Both are live git worktrees of the same repository, so they exist on disk simultaneously.
`cd miniftse-training` to work on the exercises; `cd miniftse` to consult the answer.

The original plan is `../TRAINING_PLAN.md`. Compressed 12-week track: M1, M2, M3, M5,
M6, M8, M10, M13, M15.

---

## State: it runs

```bash
cd miniftse && uv sync
make check          # ruff + mypy --strict + 70 tests, all green
make build-index    # 10y history + factsheet from a clean clone, no network, no keys
```

- **66 source files, ~18,700 lines.** `mypy --strict` clean, `ruff` clean, 70 tests pass.
- The golden master pins a 10-year index history to a hash; CI fails on any drift.
- Everything runs against a deterministic synthetic universe. No API keys, no network.

---

## What was built, and what is genuinely finished

Modules 1–3, 5–6, 8, 10, 13, 15 are implemented and wired. Beyond the plan, five index
variants build end to end from one shared universe:

| Variant | Active share | Factor exposure | Ann. turnover |
|---|---:|---:|---:|
| Parent (float cap) | — | — | 4.5% |
| Value tilt | 0.42 | 1.46 | 45% |
| Value selection (top 30%) | 0.69 | 0.98 | 60% |
| Value optimised (TE ≤ 3%) | 0.59 | 1.29 | 80% |
| Currency hedged | — | — | — |

`miniftse daily` runs a real incremental production job: loads the day, validates,
rolls the index forward one day from persisted state, validates, gates, publishes,
writes a run manifest.

### Bugs the build found — read these, they are the most instructive part of the repo

Each was found by actually running the thing, not by inspection. All are fixed, and each
is written up in the commit that fixed it.

1. **The capping factor never carried the tilt.** `C_i` was `capped/raw`, but `raw` is
   already the weighter's output, so `C ≈ 1` and the published index silently reverted
   to float-cap weights. A tilt with 0.40 active share at the review published an index
   with 0.003 active share. `C_i` must be `target/floatcap`. (`f81d052`)
2. **The optimised variant was infeasible at every review** and fell back to a tilt
   while reporting success — its weight bound sat below the parent's own concentration.
   The benchmark must be the *capped* parent so "hold the parent" is always admissible.
3. **The hedge added cumulative mark-to-market daily** instead of the daily change,
   more than doubling the hedged index over six years.
4. **Brinson was run across six years and 24 reviews**, reporting +23.8% of active
   return against an actual +10.6%. Brinson is a single-period method. (`203480b`)
5. **The price generator compounded arithmetic returns**, so σ²/2 volatility drag turned
   the reference index negative over a decade.
6. **Beta earned a linear return premium**, so the low-volatility factor came out with
   the wrong sign. The empirical security market line is flat — that flatness *is* the
   anomaly.
7. **Divisor was rebased twice on removals**; **spin-off children inherited cap factor
   1.0** instead of the parent's, breaking continuity.
8. **BLOCKED didn't propagate through the DAG**, so downstream steps ran and died with
   misleading KeyErrors. (`e358448`)

---

## What is NOT done

The goal for this build was "no lazy implementation." Three tasks remain open against
that bar. They are tracked as tasks #13 (partial), #14, #15.

### 1. Production orchestration — partially done
- ✅ Real daily DAG, incremental calculation, state file, run manifests, retry policy,
  failure injection for three modes.
- ❌ **Dagster job definitions.** The plan (M11 P11.3) asks for Dagster or Airflow.
  `production/pipeline.py` is a hand-rolled DAG; a thin Dagster wrapper over
  `DailyJob`'s steps is the remaining work.
- ❌ **`reproduce(manifest_id)` never exercised.** `production/manifest.py` implements
  it; nothing calls it and no test proves a three-month-old run regenerates exactly.
- ❌ **Reconciliation study (P12.4).** Rebuild a proxy index against a real published
  one and explain every basis point. Needs the iShares adapter (below).

### 2. Vendor providers — incomplete Protocol coverage
`data/vendors.py` has real implementations, but not all satisfy the full
`MarketDataProvider` Protocol:
- `YFinanceProvider` has prices and corporate actions; no `get_shares`, `get_fx`,
  `get_classifications`, reference methods.
- `EdgarProvider` has fundamentals only.
- `LsegDataProvider` is a **stub with commented-out call shapes**. Cannot be licensed
  here, but the code path should be real code guarded by an import check, not comments.
- `PermIdProvider.match_organisations` works but nothing enriches the security master
  with it.
- `CompositeProvider` is written but never exercised end to end.
- `ISharesProvider.implied_float_factors` and `reconstitution_diff` are written but
  never run against real files.

### 3. Documents
- ✅ Ground Rules, runbook, factsheet, risk one-pager, attribution one-pager,
  consultation paper, five client responses, stakeholder memo, AI proposal.
- ❌ Per-module memos (the plan wants ~15; there is one stub, `M1_why_yfinance_lies.md`,
  which is **Jason's exercise — do not fill it in**).
- ❌ Factor research paper, post-incident report, LSEG vocabulary map (P14.1),
  AI-assisted development retro (P13.4).

### Known measurement caveats, stated not hidden
- The risk model **over-forecasts** (bias statistic 0.62). Largely because exposures are
  held fixed at the estimation date while the index rebalances quarterly. Reported
  honestly in the risk one-pager rather than tuned away.
- The RAG assistant's eval has **5 documented failing cases** out of 40, kept as an
  honest baseline rather than deleted.
- Turnover figures are universe-size dependent. At 150 securities the 5/10/40 cap
  dominates the weighting; at 500 it barely binds. Quote numbers from the 500-name
  universe.

---

## Where to pick up

Highest value first:

1. **Vendor Protocol completeness** (task #14) — unblocks the reconciliation study and
   is the piece that makes "swap the provider, change nothing upstream" a demonstrated
   claim rather than an architectural assertion.
2. **`reproduce()` + a test** (task #13) — small, and it is the audit story an index
   provider actually gets asked about.
3. **Dagster wrapper** (task #13) — thin, mostly mechanical.
4. **Remaining documents** (task #15).

---

## Things to know before touching anything

- **Never edit `miniftse-training/`.** That is Jason's work.
- **Never fill in `memos/M1_why_yfinance_lies.md`.** It is an assigned exercise.
- The golden master will fail if you change the parent index's numbers. If that is
  intentional, regenerate it deliberately (`make golden`) and say so in the commit.
- The synthetic universe is a **simulation**. Value and quality predict returns in it
  because they were built to. No result computed on it is evidence about real markets,
  and every research output in the repo carries that caveat. Keep it that way.
- `DECISIONS.md` records every judgement call and the alternative rejected. Add to it
  rather than silently changing a threshold.
