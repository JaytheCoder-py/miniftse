# Handover

**Last session ended:** 2026-08-11 · **Branch:** `master`

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
make ci                          # ruff + mypy --strict + tests + golden master, all green
make build-index                 # 10y history + factsheet, no network, no keys
make desk-serve                  # the ops desk on :8000, from the committed snapshot
uv run miniftse documents        # regenerate every document from the code
uv run miniftse reconcile        # constituent-level reconciliation study
uv sync --extra orchestration    # then: uv run dagster dev -m miniftse.production.dagster_defs
```

- **77 source files, ~24,000 lines.** `mypy --strict` clean, `ruff` clean, **217 tests**
  (198 in a clean clone; the 19 Dagster/orchestration tests skip without the extra).
- The golden master pins a 10-year index history to a hash; CI fails on any drift.
- Everything runs against a deterministic synthetic universe. No API keys, no network.
- Verified from a fresh `git clone` + `uv sync` in this session.

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

Since the last handover the **ops desk** (`desk/`) exists: a FastAPI + HTMX
application over a precomputed snapshot of the reference build (`desk/data/`,
committed like `artefacts/`), with five screens — explain-a-day, a live chaos drill,
the methodology assistant with its eval scoreboard and guarded draft demo,
reproducibility against the pinned golden master, and the index overview with the
capacity explorer. It rebuilds nothing at startup, computes no index figure of its own
(`test_desk_contains_no_index_arithmetic` enforces that), and runs offline like
everything else. `make desk-data` regenerates the snapshot; `make desk-serve` serves
it.

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

Everything the plan describes is implemented and exercised. The three fronts the
previous handover listed as open are closed — see `85f3d0f` and `598f525`.

What remains is bounded by **access, not effort**:

- **The LSEG adapter cannot be run here.** It is real code behind an import guard —
  Datastream history, Worldscope fundamentals with point-in-time `SDate`, IBES
  estimates, ICB classification, index constituents — and raises with the specific
  missing prerequisite rather than returning empty frames. It needs a licence. The
  reshaping functions (`_normalise_prices`, `_normalise_fundamentals`) are separated
  precisely so they are testable against a recorded fixture without one.
- **No cloud deployment of the production DAG.** The Dagster job runs locally; nothing
  is deployed to AWS or Azure. The ops desk is further along: its container is
  deployable to a Hugging Face Space as-is and `desk/README.md` is the step-by-step
  runbook, but pushing it live needs the repository owner's own account, so it has not
  been done from here.
- **Free-data gaps are structural.** Free float, corporate action detail, analyst
  estimates and delisted securities have no free source. `docs/lseg_vocabulary_map.md`
  names each and what supplies it.
- **`M1_why_yfinance_lies.md` is deliberately unwritten** — it is Jason's assigned
  exercise. Do not fill it in.

### Measurement caveats — reported, not tuned away
- The risk model **over-forecasts** (bias statistic 0.62), largely because exposures are
  held fixed at the estimation date while the index rebalances quarterly. Stated in the
  risk one-pager.
- The RAG assistant's eval has **5 documented failures out of 40**, kept as an honest
  baseline.
- The reconciliation study explains **52% of a 405bp difference** against its synthetic
  comparison and says so, rather than padding a component to make the table sum.
- Turnover figures are universe-size dependent. At 150 securities the 5/10/40 cap
  dominates the weighting; at 500 it barely binds. Quote the 500-name numbers.

---

## Where to pick up

The build is complete against the plan. Natural next steps, none of them blocking:

1. **Point it at real data.** `build_free_composite()` (Yahoo + EDGAR + FRED) has full
   Protocol coverage. Running the engine against it is the strongest remaining
   validation, and the reconciliation module is built for exactly that.
2. **Archive iShares holdings daily, starting now.** The historical files are not
   retrievable, so the archive not started last year is the study that cannot be run
   today. `ISharesProvider.archive_all()` is a one-line scheduled job.
3. **Golden-master the factor variant**, not just the parent — the capping incident
   would have been caught immediately by one.
4. **Deploy something.** The ops desk has a written Hugging Face Spaces runbook
   (`desk/README.md`) waiting on the owner's account; the Dagster job still needs a
   real scheduler somewhere if the cloud module matters for interview purposes.

---

## Things to know before touching anything

- **Never edit `miniftse-training/`.** That is Jason's work.
- **Never fill in `memos/M1_why_yfinance_lies.md`.** It is an assigned exercise.
- The golden master will fail if you change the parent index's numbers. If that is
  intentional, regenerate it deliberately (`make pin-golden`) and say so in the commit.
- The synthetic universe is a **simulation**. Value and quality predict returns in it
  because they were built to. No result computed on it is evidence about real markets,
  and every research output in the repo carries that caveat. Keep it that way.
- `DECISIONS.md` records every judgement call and the alternative rejected. Add to it
  rather than silently changing a threshold.
