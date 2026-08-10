# Decision log

Every judgement call, with the reasoning and the alternative that was rejected.
An index is a chain of these. In an interview you will be asked *why*, not *what*.

Format:

```
## D-NNN — <short title>
**Date:** YYYY-MM-DD · **Module:** M<n> · **Status:** accepted | superseded by D-NNN

**Context.** What forced a choice.
**Decision.** What we do.
**Alternatives rejected.** What else was on the table and why it lost.
**Consequences.** What this now commits us to, including the bad parts.
```

---

## D-001 — src-layout package, `uv` for environment management
**Date:** 2026-08-09 · **Module:** M0 (setup) · **Status:** accepted

**Context.** The repo needs to be installable and importable identically from a clean
clone, CI, and a notebook. Notebook-relative imports are the usual failure mode.

**Decision.** `src/` layout, `uv` for locking, package installed editable.

**Alternatives rejected.** Flat layout (imports resolve against the working directory,
so tests can pass locally and fail in CI); `poetry` (fine, but slower and `uv` is
becoming the default in quant shops); conda (heavyweight, poor lockfile story).

**Consequences.** Nothing is importable until `uv sync`. `uv.lock` is committed and
is the source of truth for reproducibility — a run manifest (M10) will reference it.

---

## D-002 — `mypy --strict` from day one
**Date:** 2026-08-09 · **Module:** M0 (setup) · **Status:** accepted

**Context.** Index maths is full of quantities that are all `float` to the compiler and
all different to a human: a price, a weight, a divisor, an adjustment factor, a
free-float fraction. Confusing two of them is the classic production bug.

**Decision.** Strict typing enforced from the first commit rather than retrofitted in M10.

**Alternatives rejected.** Add typing later — retrofitting `--strict` to an untyped
codebase is a multi-day slog and usually gets abandoned.

**Consequences.** Slower to write. Every public function needs annotations. This is the
point: it forces the domain model to be explicit.

---

## D-003 — SEDOL is the primary key, not ISIN
**Date:** 2026-08-09 · **Module:** M1 · **Status:** accepted

**Context.** A global index must know which *listing* it holds: that fixes the price, the
currency and the trading calendar.

**Decision.** SEDOL first in `IdentifierSet.primary_key`, then ISIN, CUSIP, RIC, ticker.
`IDENTIFIER_LEVELS` records which level each scheme keys on, and `SecurityMaster.resolve`
raises `AmbiguousIdentifierError` rather than guessing when a security-level identifier is
asked for a listing.

**Alternatives rejected.** ISIN as primary — one ISIN spans every venue, so keying on it
silently merges the London and Frankfurt lines of the same company. Ticker — recycled and
market-specific.

**Consequences.** Some data sources supply only ISIN, and for those the master refuses
rather than picking a line. That is the intended behaviour and it surfaces as an error
rather than as a wrong price.

---

## D-004 — A deterministic synthetic universe is the default data source
**Date:** 2026-08-09 · **Module:** M1 · **Status:** accepted

**Context.** A golden-master regression test needs bit-identical inputs. Real market data
is revised continuously, so a history pinned to it fails for reasons unrelated to the
code. Separately, a clean clone must build a full index with no licence.

**Decision.** `data/synthetic.py` generates a reproducible universe from a seed: a genuine
factor structure, all sixteen corporate action types, delistings, late listings,
restatements, foreign ownership limits. Real adapters sit behind the same Protocols.

**Alternatives rejected.** Cached real data — large, licence-encumbered, and still revised.
Random data with no factor structure — cheap, but the factor and risk modules would have
nothing to find, so their correctness would be untestable.

**Consequences.** Nothing computed on it is evidence about real markets, and every
research output says so. The risk is that someone quotes a simulated Sharpe ratio as a
finding; the mitigation is the caveat in every module docstring and in the factsheet.

---

## D-005 — Capping factors are fixed at the review, not recalculated daily
**Date:** 2026-08-09 · **Module:** M2 · **Status:** accepted

**Context.** Prices move after a review, so a constituent drifts above its cap.

**Decision.** Capping binds at the review. Drift between reviews is expected. The
validation rule warns above the cap and blocks only beyond 1.5x it.

**Alternatives rejected.** Daily re-capping — generates continuous turnover for tracking
funds and defeats the purpose of a scheduled review.

**Consequences.** The index can exceed its published cap intra-review, which must be
disclosed (Ground Rules section 6.4). A first draft of `check_max_weight` treated the cap
as a daily limit and failed on clean data at every interval — which is how a check gets
ignored.

---

## D-006 — Incumbents are screened at 75% of the entry threshold
**Date:** 2026-08-09 · **Module:** M3 · **Status:** accepted

**Context.** A security oscillating around a threshold enters and leaves at alternate
reviews, generating turnover that costs money and conveys nothing.

**Decision.** `INCUMBENT_RELIEF = 0.75` on free float, liquidity and size; 50% on price
history.

**Alternatives rejected.** Symmetric thresholds — clean but demonstrably wasteful. A
time-based rule (minimum tenure) — harder to explain and gameable around the clock.

**Consequences.** Two securities of identical size can have different index status
depending on which side they came from. Path dependence, deliberately accepted.

---

## D-007 — The market factor carries no beta-scaled drift
**Date:** 2026-08-09 · **Module:** M5 · **Status:** accepted

**Context.** With drift scaled by beta, high-beta names mechanically out-earn low-beta
ones, so sorting on low volatility sorts on low beta and the low-volatility factor comes
out inverted.

**Decision.** Drift is applied uniformly; the market factor is a zero-mean shock. Each
security's log drift additionally carries minus one half sigma squared, so expected
*simple* returns differ only by the intended factor premia.

**Alternatives rejected.** Leaving the CAPM-consistent version — theoretically tidy, but
the empirical security market line is flat, and that flatness *is* the low-volatility
anomaly. A universe that prices beta linearly cannot contain the effect it claims to.

**Consequences.** The simulated world has no beta premium. Stated in the module docstring.

---

## D-008 — The publication gate has no software override
**Date:** 2026-08-09 · **Module:** M12 · **Status:** accepted

**Context.** Every gate acquires a bypass flag, and every bypass flag eventually gets set
at 6am by someone under deadline pressure.

**Decision.** `ValidationReport.may_publish` is derived, not settable. Overriding a
blocking finding is a human decision, recorded in the incident log and disclosed in the
next market notice.

**Alternatives rejected.** A `--force` flag — the entire value of the gate is that it
cannot be quietly bypassed.

**Consequences.** A false positive on a blocking rule delays publication until a person
signs. That cost is accepted: publishing late is recoverable, publishing wrong is not.

---

## D-009 — The CLI exits zero when the gate blocks
**Date:** 2026-08-10 · **Module:** M12 · **Status:** accepted

**Context.** A blocked gate is a legitimate outcome of a build, not a build failure. The
history is still computed and written.

**Decision.** `build-index` reports the block and exits 0; `--strict` exits non-zero for
production schedulers.

**Alternatives rejected.** Always exiting non-zero — conflates "the software failed" with
"the software worked and found a problem", and makes `make build-index` unusable.

---

## D-010 — Language models never produce numbers
**Date:** 2026-08-10 · **Module:** M13 · **Status:** accepted

**Context.** A model asked to explain index performance will write a fluent paragraph
containing figures it invented.

**Decision.** Code computes numbers into a `FactPack`; the model writes prose around them;
`NumberGuard` checks every numeral in the draft against the pack and blocks on anything
unaccounted for. Rounding is permitted, invention is not.

**Alternatives rejected.** Prompt instructions alone — a safety property that depends on
the model behaving is not a safety property. Post-hoc human review only — works until
volume rises.

**Consequences.** A *correct* number used in the *wrong context* still passes. Noted in
the AI proposal as the residual risk.

---

## D-011 — Graded mypy strictness: strict core, relaxed DataFrame boundary
**Date:** 2026-08-10 · **Module:** M10 · **Status:** accepted

**Context.** `mypy --strict` produced 108 errors, of which 35 were missing third-party
stubs and most of the rest were pandas interop: `float(df.iloc[i]["col"])` is typed by
pandas-stubs as a fourteen-member union including `bytes` and `timedelta`.

**Decision.** Strict globally. Per-module overrides disable only the DataFrame-interop
error codes in modules that read values out of frames, with a stated reason. The pure
domain modules — types, config, calc.state, weighting, secmaster, universe.banding — stay
fully strict, and that is where the NewType discipline actually prevents a defect.

**Alternatives rejected.** Full strict compliance — reachable only by casting on every
line, and a cast asserts a type rather than checking one, so it is strictly worse than
not annotating. Global `strict = false` — abandons the checking where it pays.

**Consequences.** "mypy clean" in this repo means what this decision says, not what
`--strict` means unqualified. The README says so rather than implying full compliance.

---

## D-012 — Deterministic seeding everywhere, never from `hash()`
**Date:** 2026-08-10 · **Module:** M10 · **Status:** accepted

**Context.** The spin-off child RNG was seeded from `hash(spin_id)`. Python randomises
string hashing per process, so two identical builds produced different spinco price
series. It survived the small-universe determinism test because that window contains no
spin-offs, and was caught by the golden master: 5.3bp of drift between two builds with
identical code, config and inputs.

**Decision.** Seeds derive from the config seed plus a positional index. `PYTHONHASHSEED=0`
in CI and in the container.

**Alternatives rejected.** Setting `PYTHONHASHSEED` alone — fixes the symptom in
controlled environments and leaves the defect for anyone running locally.

**Consequences.** This is the defect class run manifests exist to surface. `explain_diff`
now names it explicitly when outputs move with everything else unchanged.

---

## D-013 — Constituents with no price for 20 sessions are dropped at review
**Date:** 2026-08-10 · **Module:** M12 · **Status:** accepted

**Context.** Screens run on cut-off data, so a security that stops trading between the
cut-off and the effective date still passes them. If its delisting event fired on a day it
was not a constituent, that event was skipped, and the security then sat in the index at a
carried price indefinitely. The `constituents_priced` validation rule caught it on a live
build.

**Decision.** At each review, a security with no price for more than 20 trading sessions is
removed. Below the threshold the last price is carried, which is the correct treatment for
a suspension that will resolve.

**Alternatives rejected.** Removing on the first missing print — crystallises a price at
which nobody can trade, and a fund tracking the index cannot follow. Never removing — the
defect this fixes.

**Consequences.** The threshold matches Ground Rules section 5.7, where a suspension beyond
20 days stops being a suspension and becomes a valuation question for the Committee.
