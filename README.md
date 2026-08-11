# miniftse

A rules-based global equity index platform: security master, corporate actions,
divisor-based calculation, factor variants, a Barra-style risk model, a constrained
optimiser, a validation gate, and the production scaffolding an index provider actually
needs — run manifests, golden-master regression tests, and an orchestrated daily DAG.

Built as a reference implementation against a 12-week training plan for index research
and design.

```bash
make setup && make test && make build-index
```

A clean clone produces a ten-year index history with no API keys, no network and no
vendor licence. That is deliberate — see [D-004](DECISIONS.md).

---

## Ops desk

A small FastAPI application over the library above — not a second implementation of
it. Every figure the desk renders is either read straight from the precomputed
snapshot or a direct library call; a dedicated test
(`tests/test_desk.py::test_desk_contains_no_index_arithmetic`) greps every desk
source file for the re-derivation patterns (`* 100`, bps/fraction conversions, `*
252`, nth roots) that would create a second source of truth for a published number,
and fails the build if one appears. Five screens: **explain a day** (the divisor and
market moves behind one session's level), a **chaos-drill console** (re-run any of
12 injected data faults live), the **methodology assistant** (ask the rulebook, an
eval report, and a client-response drafter with a number guard that catches
unverified figures), **reproducibility** (this build checked against the pinned
golden master), and **the index** (overview, constituents, capacity, risk &
attribution).

```bash
make desk-data    # precompute the snapshot every screen serves (~30s)
make desk-serve   # run the desk locally at http://localhost:8000
```

Deploys as a Docker container — `docker build --target desk .`, serving on port 7860
(the Hugging Face Spaces convention). `desk/data/` is committed to the repo like
`artefacts/`, so the image needs no build-time index calculation, just a `COPY`. See
[`desk/README.md`](desk/README.md) for the Hugging Face Spaces deployment steps.

**Deployed URL:** _TODO — not yet deployed. Deployment requires the repo owner's own
Hugging Face account; see `desk/README.md` for the steps to run it._

---

## What it does

**Reference index over 2016–2026** (500 securities, quarterly reviews):

| | |
|---|---|
| Annualised return (GTR) | +10.4% |
| Annualised volatility | 17.7% |
| Maximum drawdown | −32.6% |
| Divisor events | 7,335 |
| Divisor continuity breaches | **0** |
| Reviews | 42 |

| Gate | Result |
|---|---|
| Tests | 70 passing — hand-computed, property-based, golden master |
| `ruff` | clean |
| `mypy` | clean (strict on the core; see [D-011](DECISIONS.md)) |
| Validation | 27 rules; 11/11 injected faults detected |
| Methodology assistant | 88% accuracy, 100% citation precision, 0% hallucinated numbers |

---

## Layout

```
src/miniftse/
  types.py        NewType domain primitives - a Weight is not a float
  config.py       every published threshold in one place
  data/           provider Protocols, deterministic universe, DuckDB PIT store, vendors
  secmaster/      issuer -> security -> listing, bitemporal resolution, check digits
  corpactions/    16 event types, TERP, apply(event, state) -> divisor adjustment
  calc/           divisor state, daily loop, PR/GTR/NTR, FX and hedging
  universe/       eligibility screens, size bands with buffers
  review/         cut-off / announcement / effective calendar, fast entry
  weighting/      six schemes, convergent capping, UCITS 5/10/40
  factors/        seven publishable factors, cross-sectional pipeline
  research/       Fama-MacBeth, Newey-West, IC decay, multiple testing
  risk/           covariance estimators, Barra-lite factor model, bias tests
  optim/          declarative constraints, infeasibility diagnostics, pricing
  attrib/         Brinson-Fachler, factor attribution, backtest-to-live bridge
  quality/        27 validation rules, publication gate, chaos drill
  production/     run manifests, daily DAG, golden master, build orchestration
  agents/         RAG assistant, triage agent, client drafter, eval harness

ground_rules/     the published methodology, and generated factor definitions
communications/   consultation paper, five client responses, stakeholder memo
docs/             AI proposal, operational runbook, SQL cookbook, presentation
tests/            unit, property, integration, and the pinned golden master
```

`DECISIONS.md` records every judgement call and the alternative rejected.

---

## Commands

```bash
make build-index       # full history through the publication gate
make chaos-drill       # inject data faults, report validation coverage
make check-golden      # rebuild and compare against the pinned history
make factsheet         # client-facing factsheet
make daily             # run the production DAG
make daily-blocked     # same, simulating an outlier that blocks publication
make evals             # methodology assistant evaluation suite
make ci                # lint, typecheck, test, golden master
```

---

## Design decisions worth knowing

**A deterministic synthetic universe is the default data source.** Real data is revised,
so a golden master pinned to it is meaningless; and a clean clone must build a full index
with no licence. The universe has a genuine factor structure, all sixteen corporate
action types, delistings, late listings and restatements. Real adapters — yfinance,
SEC EDGAR, iShares, PermID, and an `lseg.data` stub — live behind the same Protocols.

**Nothing computed on the synthetic universe is evidence about real markets.** Value and
quality predict returns in it because they were built to. It exercises machinery; it does
not discover anything.

**No language model ever produces a number.** Numbers come from code; the model arranges
prose around values it is handed, and `NumberGuard` checks every numeral in a draft
against the supplied facts. Nothing in `agents/` touches the index calculation path.

---

## Bugs the tests found

Kept as a list because it is the most honest summary of what the test suite is for.

| Found by | Defect |
|---|---|
| First end-to-end run | The divisor was rebased twice on removals — handler and dispatcher both did it |
| First end-to-end run | Spin-off children inherited capping factor 1.0 instead of the parent's, breaking continuity |
| First end-to-end run | The price generator compounded arithmetic returns, so σ²/2 drag turned the index negative over a decade |
| First end-to-end run | Eligibility used a 250-calendar-day window against a 200-session presence test — unsatisfiable, rejecting the entire universe |
| Factor IC signs | Beta earned a linear return premium, so low-volatility came out inverted. The empirical security market line is flat; that flatness *is* the anomaly |
| Factor IC signs | Log compounding gave high-vol names ~7%/yr of free Jensen convexity, swamping every configured premium |
| Hypothesis | Capping raised when the uncapped names carried ~zero mass |
| Unit test | The UCITS limb-2 binary search ran in the wrong direction — a breach made it try a *higher* cap, then report success |
| Golden master | The spinco RNG was seeded from `hash()`, which Python randomises per process. Two identical builds differed by 5.3bp |
| Chaos drill | `fx_sanity` passed an inverted rate of 9.27, because 9.27 is a plausible number |
| Chaos drill | `max_weight` treated the cap as a daily limit, so it failed on clean data at every review interval |
| Validation gate | A security re-selected at a review after its price series ended stayed in the index forever at a carried price |

---

## Scope and honest limits

Everything the plan describes is implemented and exercised. What remains is bounded by
access, not by effort:

- **The LSEG adapter cannot be run here.** It is real code behind an import guard —
  Datastream history, Worldscope fundamentals with point-in-time `SDate`, IBES
  estimates, ICB classification, index constituents — and it raises with the specific
  missing prerequisite rather than returning empty frames. It needs a licence.
- **No cloud deployment.** The container builds and the DAG runs; nothing is deployed
  to AWS or Azure.
- **Free-data gaps are structural, not oversights.** Free float, corporate action
  detail, analyst estimates and delisted securities have no free source. The
  `lseg_vocabulary_map.md` names each one and what would supply it.
- **Measurement caveats are reported, not tuned away.** The risk model over-forecasts
  (bias statistic 0.62), largely because exposures are held fixed while the index
  rebalances quarterly. The RAG eval has 5 documented failures out of 40. Both are kept
  as honest baselines.

