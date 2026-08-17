# Handover

**Last session ended:** 2026-08-14 · **Branch:** `master`

---

## What this repo is

A complete, working rules-based index platform on `master` — security master, corporate
actions, divisor-based calculation, factor variants, a risk model, a validation gate, and
the production scaffolding around them. Read it, run it, break it.

Everything is agent-implemented and owner-reviewed. That makes the repo two artefacts at
once: an index platform, and a record of what verifying AI-written code actually costs.
`docs/ai_development_retrospective.md` is the account of the second, and the eight
locally-plausible/globally-wrong defects it documents are the reason the checks described
below exist in the shape they do.

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

# real data (network; SEC requires a contact address)
uv run miniftse fetch-real --contact you@example.com --securities 200
uv run miniftse build-index --universe data/snapshots/real-clean
```

- **86 source files.** `ruff` clean, **343 tests** (2 skipped here; the Dagster tests skip
  without the orchestration extra). Nine of those are `tests/test_readme_figures.py`,
  which pins the README's published numbers to the run manifest — added after the README
  was found quoting a +10.4% annualised return against an actual +3.7%.
- The golden master pins a 10-year *synthetic* index history to a hash; CI fails on drift.
- The **default** build is still the deterministic synthetic universe — no API keys, no
  network. Real data is opt-in via `--universe`, and is a separate path with its own
  provenance (see D-018 and `docs/superpowers/specs/2026-08-13-real-universe-design.md`).
- ✅ **`make ci` passes in full**, verified 2026-08-17: `ruff check` and
  `ruff format --check` clean, `mypy --strict` clean on 86 source files, **343 passing and
  2 skipped**, and the golden master matches across 2,311 dates to 0.0000bp on every
  column.
- ✅ **`make ci` and the GitHub workflow now run the same checks.** They did not: the
  workflow's lint job ran `ruff format --check src tests` and the Makefile's `lint` target
  ran only `ruff check`, so a green `make ci` sat on a tree GitHub Actions rejected — 64
  files out of format, all pre-existing. Both the divergence and its symptom are fixed:
  `lint` runs the format check too, and the 64 files are formatted. The golden master
  re-verified afterwards, since a formatting sweep across the calculation code is exactly
  the change you want a regression pin to have an opinion about.

---

## What was built, and what is genuinely finished

Every stage of the platform is implemented and wired, and five index variants build end
to end from one shared universe:

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
   with 0.003 active share. `C_i` must be `target/floatcap`. (`df89802`)
2. **The optimised variant was infeasible at every review** and fell back to a tilt
   while reporting success — its weight bound sat below the parent's own concentration.
   The benchmark must be the *capped* parent so "hold the parent" is always admissible.
3. **The hedge added cumulative mark-to-market daily** instead of the daily change,
   more than doubling the hedged index over six years.
4. **Brinson was run across six years and 24 reviews**, reporting +23.8% of active
   return against an actual +10.6%. Brinson is a single-period method. (`b55c944`)
5. **The price generator compounded arithmetic returns**, so σ²/2 volatility drag turned
   the reference index negative over a decade.
6. **Beta earned a linear return premium**, so the low-volatility factor came out with
   the wrong sign. The empirical security market line is flat — that flatness *is* the
   anomaly.
7. **Divisor was rebased twice on removals**; **spin-off children inherited cap factor
   1.0** instead of the parent's, breaking continuity.
8. **BLOCKED didn't propagate through the DAG**, so downstream steps ran and died with
   misleading KeyErrors. (`c296af6`)

---

## What is NOT done

The platform is complete and exercised end to end. The three fronts the
previous handover listed as open are closed — see `507e389` and `b4b71aa`.

What remains is bounded by **access, not effort**:

- **The LSEG adapter cannot be run here.** It is real code behind an import guard —
  Datastream history, Worldscope fundamentals with point-in-time `SDate`, IBES
  estimates, ICB classification, index constituents — and raises with the specific
  missing prerequisite rather than returning empty frames. It needs a licence. The
  reshaping functions (`_normalise_prices`, `_normalise_fundamentals`) are separated
  precisely so they are testable against a recorded fixture without one.
- **No cloud deployment of the production DAG.** The Dagster job runs locally; nothing
  is deployed to AWS or Azure. The ops desk is further along: its container is
  deployable to Google Cloud Run as-is and `desk/README.md` is the step-by-step runbook,
  but pushing it live needs the repository owner's own account, so it has not been done
  from here. The runbook targeted Hugging Face Spaces until 2026-08-12, when Hugging Face
  moved Docker Spaces behind a paid plan; `DECISIONS.md` D-016 records the switch and
  D-017 the forwarded-headers defect it surfaced.
- **Free-data gaps are structural.** Free float, corporate action detail, analyst
  estimates and delisted securities have no free source. `docs/lseg_vocabulary_map.md`
  names each and what supplies it.
- **Module 1 has no memo.** What it would have covered — why a free price API cannot
  supply historical market capitalisation — lives in the `vendors.py` provider docstrings
  and `data/real.py` instead, next to the code that has to cope with it.

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

## Corporate action triage — stage 1 complete (2026-08-14)

`src/miniftse/triage/` is new: it reads corporate action announcements, produces the
structured `CorporateAction`, and grades the result in **basis points of index impact**
rather than classification accuracy. The argument is that accuracy scores a misread
dividend amount the same as a return of capital booked as an ordinary dividend, and those
differ by two orders of magnitude in what they do to a published level.

Seven modules: `taxonomy` (pins the 16-value label space to its handler classes),
`verify` (the grader), `corpus` (labelled announcements with provenance, JSONL),
`labels` (free labels from a vendor feed), `text` (SEC filing text and the label join),
`extract` (the LLM layer). 82 tests. Spec in
`docs/superpowers/specs/2026-08-13-corporate-action-triage-design.md`, decisions D-021
through D-033.

**Not to be confused with `miniftse.agents.triage`**, which triages data-quality alerts.
Different thing, colliding name.

### Stage-2 carry-forward — read this before extending it

Four items were deliberately deferred. None is a defect in what shipped; all four will
bite whoever builds the stage-2 eval harness.

1. **`make_state()` builds three *identical* constituents**, so relocating an event from
   one to another is invisible to `error_bps` — the whole `TestImpactError` suite is
   structurally blind to that class of defect, which is why a payload-can-set-`security_id`
   bug survived eight reviews. The fix is a **separate weighted fixture** for
   relocation-sensitive tests. Do **not** change `make_state`'s defaults: six tests and
   three docstrings pin the canonical 67.114bp derivation to its equal weights.
2. **A wrong split ratio is graded zero, and that is correct but incomplete.** A split is
   market-value-invariant by construction, so the ex-date impact genuinely is zero in both
   level and divisor. But `shares` persists in the daily state and `_mark` refreshes only
   `price`, so the error surfaces at the next mark-to-market — roughly 2,666.67bp on the
   three-name fixture — and never self-corrects. The fix needs no new arithmetic: apply
   both events, stamp the truth-side post-event price onto both states, diff the two
   engine-produced levels. Roughly an hour. It belongs in its own field, never folded into
   the headline `max()`. See D-029's *Alternatives*.
3. **Many-to-one joins inflate the error distribution by an unbounded amount.** When two
   labels join to the same filing, both `Announcement`s carry byte-identical text under
   contradictory labels, so a *correct* extractor is scored wrong on at least one — and
   that bps figure measures the join heuristic, not the model. D-025 records three costed
   options; a `shared_document` flag is the cheapest first step.
4. **Identifier hallucination is now silently corrected and therefore unmeasured.** A
   payload can no longer set `security_id`/`event_id` (the caller's win), which is right
   for grading — but it means a model inventing an identifier leaves no trace in the
   score. Stage 2 should capture that signal in `extract.py`; the raw model output is
   already on `Extraction.raw`.

**Also outstanding:** the real-model smoke test (Step 5 of the stage-1 plan) has never
been run. It needs `ANTHROPIC_API_KEY`, network and spend, and `anthropic` is deliberately
not a dependency — `AnthropicLlm` imports it lazily and raises a clear error. Everything
in `triage/` is tested against a scripted client, so **no line of this package has ever
met a real model.**

---

## Where to pick up

The build is complete. Natural next steps, none of them blocking:

1. **Point it at real data.** `build_free_composite()` (Yahoo + EDGAR + FRED) has full
   Protocol coverage. Running the engine against it is the strongest remaining
   validation, and the reconciliation module is built for exactly that.
2. **Backfill iShares holdings — history is retrievable after all.** This entry
   previously said the opposite and recommended starting a daily archive. That was
   wrong: `ISharesProvider.fetch_holdings(ticker, as_of)` now reads any past date,
   verified to 2008-06-30 for IWM. `archive_all()` is a backfill to walk backwards, not
   a job to schedule forwards. The old `.ajax` endpoint it used had also died in the
   worst way — HTTP 200, `Content-Type: text/csv`, HTML body — so anything calling it
   would have parsed a web page into a DataFrame without raising. Nothing did call it,
   so no published number was affected, but that is luck rather than design and the
   guard clauses are now explicit.
3. **Golden-master the factor variant**, not just the parent — the capping incident
   would have been caught immediately by one.
4. **Deploy something.** The ops desk has a written Google Cloud Run runbook
   (`desk/README.md`) waiting on the owner's account — a source deploy, so it needs no
   git remote, which is the one prerequisite this repository still lacks. The Dagster job
   still needs a real scheduler somewhere if the cloud module matters for interview
   purposes.

---

## Things to know before touching anything

- `memos/README.md` is **generated** by `reporting/memos.py`, as is every memo beside it.
  Edit the generator and re-run `uv run miniftse documents`; a hand edit is reverted on
  the next run.
- The golden master will fail if you change the parent index's numbers. If that is
  intentional, regenerate it deliberately (`make pin-golden`) and say so in the commit.
- The synthetic universe is a **simulation**. Value and quality predict returns in it
  because they were built to. No result computed on it is evidence about real markets,
  and every research output in the repo carries that caveat. Keep it that way.
- `DECISIONS.md` records every judgement call and the alternative rejected. Add to it
  rather than silently changing a threshold.
