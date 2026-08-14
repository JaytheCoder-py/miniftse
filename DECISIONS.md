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

---

## D-014 — Band assignment is decided on exact, quantised arithmetic with a written tie-break
**Date:** 2026-08-11 · **Module:** M3 · **Status:** accepted

**Context.** *(Divergence figures in this paragraph are as recorded in the ops-desk design
spec, `docs/superpowers/specs/2026-08-11-ops-desk-design.md` (2026-08-11). This repository
holds no independent record of that incident and observing it requires two platforms, so
unlike the re-pin numbers below they are reproduced from that document rather than
re-measured here.)*

The golden master was pinned on Windows. A rebuild on Linux from the same
commit, config and inputs produced 153 constituents at the 2024-09-20 review where the
master had 154. The rebase maths was correct — `level_continuity_bps` was 0.0, the divisor
absorbed the change, no validation rule fired — but a divisor that has absorbed a different
constituent set is a different divisor from then on. The two builds diverged by up to
0.7426bp on the divisor, with 65 of 2311 dates carrying a different constituent count:
below the 0.1bp golden tolerance on levels, so CI stayed green, and well above zero, which
is what it should have been.

Three lines were responsible. `universe/banding.py:81` computed the cumulative percentile
with `np.cumsum`, which sums in blocks whose size and association depend on the SIMD width
and the BLAS build, so the last bits differ across platforms. Line 112 compared it to the
band cutoff with a bare `<=` carrying no tolerance and no tie-break, so a security sitting
on the cut landed on opposite sides on the two machines. Line 142's buffer-zone test had
the same exposure and had simply not fired yet. Because bands are cut on the *cumulative*
share, a last-bit difference is not confined to one security — it shifts every name below
it, and one name near a cutoff moves the whole boundary.

A fourth defect was found while fixing these, and is worth recording because it needed no
platform difference at all: the sort was `sorted(..., key=cap, reverse=True)`, which is
stable but only a *partial* order. Two securities of exactly equal float market cap ranked
in whatever order the caller's dict iterated. A permutation test caught it immediately on
Windows alone.

**Decision.** Band assignment is a rule, and a rule whose outcome depends on floating-point
summation order is not a rule. Four changes, all in `universe/banding.py`:

1. **Exact summation.** `_exact_cumulative` replaces `np.cumsum`, using Shewchuk's exact
   partial-sum algorithm — the one behind `math.fsum`, kept open so the running total can
   be read after each element rather than only at the end. Every prefix is the correctly
   rounded value of the exact mathematical sum, so it depends on the values alone and never
   on how the machine associated the additions. It is O(n·k), where k is the number of
   distinct exponent ranges in play (small in practice), against O(n²) for
   `math.fsum(values[:i+1])` per element, and the tests assert the two agree bit for bit.
2. **A total order.** Ties in float market cap break on ascending `security_id`. Arbitrary,
   but written down, which is the property that matters.
3. **Quantisation to 12 decimal places** of cumulative weight before any comparison against
   a cutoff or a buffer edge. `CUMULATIVE_PCT_DECIMALS` in the code, Ground Rules 2.1.1, and
   this entry — three places, because a precision that lives only in code is not a rule
   anyone can hold us to.
4. **The tie-break written into the ground rules.** 2.1.1: a security exactly on a cutoff
   belongs to the band that cutoff closes. 8.3: exactly one buffer width past a boundary is
   not *more than* one buffer width, so the incumbent is held.

**Why 12.** An exactly rounded sum carries at most half an ulp of error, around 1e-16 on a
cumulative share of order 1. Twelve decimal places sits four orders of magnitude above that
noise floor, so quantisation can never mask a real difference; and 1e-12 of cumulative
index weight is 1e-8 of a basis point, seven orders of magnitude finer than the golden
master's own 0.1bp tolerance. It buys the thing that actually matters: "exactly on the
cutoff" becomes a decidable state with a documented outcome instead of a coin-flip settled
by a last bit.

**Alternatives rejected.** *Kahan/Neumaier compensated summation* — O(1) per element and
deterministic for a fixed order, but only approximately exact, so it still leaves a
last-bit argument to have; `math.fsum` semantics are exact and need no caveat, and the
performance difference is nil at universe sizes in the hundreds. *An absolute epsilon on
the comparison* (`cum <= cutoff + 1e-9`) — moves the arbitrary boundary rather than
removing it, and an epsilon in a comparison is undocumentable in a client-facing rulebook
in a way that "rounded to 12 decimal places" is not. *Widening the golden tolerance until
the platforms agree* — would have hidden the defect, which is what the tolerance had
already effectively done. *Pinning numpy and declaring the build Linux-only* — makes
reproducibility a property of the environment rather than of the methodology, and index
rules must be reproducible by a client who is not running our container. *Leaving the
tie-break to the code* — the actual root cause; the ground rules said "the top 70% by
value" and left the boundary case undefined, so when two platforms disagreed there was no
document that said which was right.

**Consequences.** Band assignment is now bit-for-bit reproducible across platforms, numpy
versions and BLAS builds, and `assign_bands` is a pure function of the universe rather than
of the dict that carried it. The published `cumulative_pct` on each `BandAssignment` is the
quantised value the rule was decided on, so a diagnostic can no longer disagree with the
assignment it is explaining. Two securities of identical size are now separated by an
alphabetical accident rather than an insertion-order accident — no better economically, but
stable and stated.

**What it actually changed, and the second finding.** The fix moved the parent index, so the
golden master was re-pinned deliberately in its own commit. Across all 37 reviews in the
2016–2024 reference history the fix changed exactly **one** band assignment, and it was not
the `np.cumsum` limb that did it — the cumulative percentiles were bit-identical before and
after on this machine, as expected, since the divergence that opened the incident needed two
*different* platforms to show itself.

It was the buffer-edge comparison at the old line 142, the one added to this fix on the
grounds that it "had the same exposure and had simply not fired yet". It had fired. At the
December 2024 review, SEC00012 was the smallest name in the universe, so its cumulative
share was exactly 1.0 — the last-ranked security's always is. The Small Cap boundary is 0.98
and the buffer is 0.02, and `0.98 + 0.02 == 1.0` exactly, so it sat exactly one buffer width
out. `abs(1.0 - 0.98)` evaluates to 0.020000000000000018, because 0.98 has no exact binary
representation, so the old `<= width` was false and the security was dropped to Micro Cap
and out of the All Cap index. Under Ground Rules 8.3 as now written it is held in Small Cap.

**This was not a knife-edge, and the distinction matters.** 0.020000000000000018 is not a
value that varies between machines or runs — it is what IEEE-754 double arithmetic gives for
that subtraction, everywhere, every time. The old comparison failed *by construction*: every
Small Cap incumbent that was also the smallest name in the universe was dropped to Micro Cap,
reliably. What is rare is the *configuration* — a Small Cap incumbent that is simultaneously
last-ranked and would otherwise fall to Micro — which arose once in 37 reviews. Left
unfixed, recurrence was guaranteed the next time it arose. That is worse than a coin-flip,
not better: a coin-flip at least announces itself by giving two different answers to the
same question, where this returned the same wrong answer every time and so looked like a
rule.

(An earlier draft of this entry and of the `/reproducibility` page described this as "0.6
ulp" and framed it as a freak knife-edge. That was wrong in the direction that flattered the
old code, and is corrected here rather than quietly reworded.)

One security, one review. The divisor rebased to absorb it exactly as it
should — the levels moved 0.0115bp — and the divisor is 0.4534bp different from that review
onward, across the final 5 of 2311 dates. The same shape as the incident that started this,
found on one machine, by fixing a line nobody had yet seen fail. It is the argument for
fixing a defect class rather than a defect.

**A structural consequence, recorded and deliberately not fixed.** Because
`small_cutoff + buffer_width == 0.98 + 0.02 == 1.0` exactly, the entire Micro Cap band lies
within one buffer width of the Small/Micro boundary. Post-fix, that buffer can therefore
never release anything: a Small Cap incumbent whose hard band is Micro is *always* inside
the buffer and is always held. Pre-fix the only escape was the representation error
described above — that is, the defect. This follows from the band boundaries and buffer
width the Ground Rules specify, not from this fix; changing it would be a methodology change
requiring consultation under M15 and is out of scope here. It is written down so the record
does not imply the Small/Micro buffer does something it cannot.

The parent index's numbers are permitted to move for a defect fix, but only in a commit that
says so.

---

## D-015 — The drill baseline resolves its config from the manifest, by name
**Date:** 2026-08-11 · **Module:** M12 · **Status:** accepted

**Context.** `baseline_from_build` read `config=result.manifest.config and
global_all_cap()`. `RunManifest.config` is `IndexConfig.to_dict()` output — a dict, and
truthy for any real build — so the expression always evaluated to `global_all_cap()`: the
right answer for both real callers (the CLI drill and the desk snapshot both build the
default spec), but by an accident of `and`-semantics, not by decision. Flagged in the
ops-desk final review as a non-blocking follow-up.

**Decision.** Resolve the config by matching the manifest's serialised dict against the
named constructors (`ValidationContext._CONFIG_CONSTRUCTORS`, the same closed set
`save()`/`load()` already round-trips a config name through), and raise on a dict none of
them produces.

**Alternatives rejected.** *Hard-coding `config=global_all_cap()` with a comment* —
preserves today's behaviour exactly, but bakes the accident in: a `global_large_mid`
build's drill would keep reporting itself validated against the wrong index's config, now
deliberately. *Passing `None`* — discards information the manifest genuinely carries, and
silently downgrades every config-aware check in the drill to a skip.

**Consequences.** Behaviour is unchanged for every existing caller — each builds the
default spec, which resolves to `global_all_cap()` as before. A non-default named build
now resolves to its own config instead of being mislabelled; an inline config fails loudly
at baseline assembly, the same boundary `ValidationContext.save` already enforces.

---

## D-016 — The ops desk deploys to Google Cloud Run, not Hugging Face Spaces
**Date:** 2026-08-12 · **Module:** M12 (ops desk) · **Status:** accepted, supersedes the
Deployment section of `docs/superpowers/specs/2026-08-11-ops-desk-design.md`

**Context.** The design spec chose Hugging Face Spaces on the Docker SDK, free CPU-basic
tier. Hugging Face has since moved Docker and Gradio Spaces behind PRO —
<https://huggingface.co/pricing> lists "Host ZeroGPU, Gradio & Docker Spaces" as a $9/month
feature, and the new-Space form offers Static as the only free SDK. The written runbook
described a path that no longer exists.

**Decision.** Deploy the same `desk` image stage to Google Cloud Run, scaled to zero,
from a local source deploy (`gcloud run deploy --source .`). `desk/README.md` is the
rewritten runbook.

**Alternatives rejected.** *Hugging Face PRO ($9/month)* — the runbook works verbatim and
there is no cold-start penalty, but paying a subscription to host a read-only 140 MB
container that serves a 852 KB snapshot is the wrong shape of answer. *A free Static
Space* — free on the SDK that is actually available, but the desk does not survive the
translation: ten of the desk's twelve routes are pure snapshot reads and would export to
files (`/draft/render` among them — its input space is a closed question set crossed with
one boolean), while `/ask/query` runs live retrieval over `ground_rules/`+`memos/` in
Python and `/chaos/run` re-runs the validation engine against a fresh baseline. Losing
both leaves a screenshot of an ops desk rather than an ops desk. *Render's free tier* — the design spec had already
rejected Render because "Render sleeps and a cold start loses the visitor", and that
reasoning survives: Render's free tier spins down after 15 minutes and takes roughly a
minute to wake, against a Cloud Run cold start of an image pull plus a snapshot load the
suite already pins under two seconds. Cloud Run is the option that honours the spec's own
criterion at zero cost, which is why it wins over the platform the spec named.

**Consequences.** A billing account must be attached to the project even though the
service is expected to stay inside the always-free tier, so `--max-instances` and a
billing budget alert are part of the runbook rather than optional hygiene. Deployment no
longer requires a git remote at all — a source deploy uploads the working directory —
which decouples shipping the desk from the still-unresolved question of where this
repository's origin lives. The port is configured at deploy time (`--port 7860`) instead
of in the image, so the container stays portable to any host that reads `EXPOSE`. Cloud
Build, like a Hugging Face Space, cannot be handed a `--target`, so the property that
made the HF plan work — `desk` being the last stage in the Dockerfile — is still load-
bearing and must not be disturbed.

---

## D-017 — `--forwarded-allow-ips=*` is not safe on an appending proxy, and Cloud Run appends
**Date:** 2026-08-12 · **Module:** M12 (ops desk) · **Status:** accepted

**Context.** The `desk` stage's `CMD` passes `--proxy-headers --forwarded-allow-ips=*`, and
its comment justified trusting every hop on the grounds that "there is no public ingress
that lets an outside client set that header directly and have uvicorn believe it". That
claim only holds if the fronting router *replaces* `X-Forwarded-For`. Routers generally
append, and Google Cloud specifically appends the caller's address to whatever the caller
already sent. uvicorn's `_TrustedHosts.get_trusted_client_address` returns
`x_forwarded_for_hosts[0]` — the leftmost, caller-supplied entry — whenever `always_trust`
is set, so `request.client.host` becomes attacker-chosen and `limits.py`'s per-IP token
bucket keys on it.

Measured against a real uvicorn (0.52.1) running the deployed flags, not reasoned from the
source alone: 65 requests under one fixed forged header gave 60 × 400 then 5 × 429, the
bucket working as designed; 65 requests rotating the forged header gave 65 × 400 and *zero*
429s; and a forged value with a real peer appended after it — the shape an appending proxy
actually produces — resolved to the forged one.

**Decision.** Correct the claim wherever it is written down (the `CMD` comment,
`limits.py`'s `enforce_rate_limit` docstring), treat `--max-instances` as the real spend
guard on a per-request-billed platform, and put a reproduction of the probe in
`desk/README.md` so the deployed service is measured rather than assumed. The flag itself
is left as-is pending that measurement.

**Alternatives rejected.** *Changing `--forwarded-allow-ips` to a guessed peer address now*
— it is the right shape of fix (uvicorn walks the header from the right and returns the
first untrusted entry once it is trusting a specific host rather than everything), but the
correct value and the hop count depend on what Cloud Run actually presents to the
container, and guessing the hop count is precisely how this class of bug is reintroduced.
*Dropping `--proxy-headers` entirely* — restores a trustworthy key, but it is the proxy's
address for every visitor, so the limiter degrades from "no limit" to "one shared 60/minute
budget", letting a single caller lock everyone else out. *Leaving it undocumented on the
grounds that the desk is read-only* — the exposure is real even if the blast radius is
money rather than data, and a comment asserting a safety property the code does not have is
worse than no comment.

**Consequences.** The per-IP rate limiter is documented as best-effort until the deployed
hop shape is measured; `--max-instances 2` and a billing alert are what actually bound the
cost. The probe in `desk/README.md` is the acceptance test for any future change to the
forwarded-headers configuration.

---

## D-018 — The materialised snapshot is the universe interface, not the generator
**Date:** 2026-08-13 · **Module:** M1 (data) · **Status:** accepted

**Context.** `data/providers.py` claimed the index engine binds to Protocols and never to
a vendor, so the synthetic generator could be swapped for real data without touching
anything upstream of `data/`. That was true of `calc/`, `factors/` and `risk/` and false
of `production/`: six call sites read `SyntheticUniverse._generated[...]` — a private dict
— and four modules annotated against the concrete class. `variants` and `daily` also
constructed their own universe internally, which additionally assumes construction is free
and deterministic. A real provider could satisfy every documented method and still not be
substitutable.

**Decision.** Promote the layout `SyntheticUniverse.materialise()` already wrote — nine
parquet tables plus a fingerprint — from an output format to *the* interface. `UniverseData`
extends `MarketDataProvider` with the whole-panel accessors, calendar, span and identity
that `production/` actually needs; `MaterialisedUniverse` loads a snapshot; `data/real.py`
writes one from SEC, Yahoo and FRED. `BuildSpec.universe` defaults to `None`, which
generates a synthetic universe exactly as before.

**Alternatives rejected.** *An adapter mimicking `SyntheticUniverse`, `_generated`
included* — smallest diff, but it makes a private attribute part of the permanent contract
and would force `_generated` into the Protocol, which contradicts D-002's premise that
types exist to make the domain explicit. *A research-only real-data study* — zero risk to
the 217 tests, but the engine never actually runs on real data, which was the point.
*Replacing the synthetic universe outright* — loses clean-clone builds, the golden-master
hash, and deliberately-placed corporate-action pathologies, all three of which
`synthetic.py` exists to provide.

**Consequences.** Real builds are reproducible because fetching is separated from
building: `data/real.py` touches the network and emits files, and a build only ever reads
files. Snapshot identity is content-addressed, so re-fetching revised data changes the
fingerprint rather than silently changing an answer — and a frozen real snapshot can now
be golden-mastered like the synthetic one. The cost is that `UniverseData` is a wider
Protocol than `MarketDataProvider`, so a thin vendor adapter is no longer sufficient to
drive a build; it must be materialised first. That is the correct trade: the engine
genuinely needs the panel, and pretending otherwise is what produced the private-attribute
coupling in the first place.


---

## D-019 — A real snapshot needs a coverage floor and an atomic write
**Date:** 2026-08-13 · **Module:** M1 (data) · **Status:** accepted

**Context.** The first real snapshot built cleanly: 199 securities, 494k price rows, an
index history that passed the publication gate. A later re-run of the same command was
rate-limited by Yahoo, which does not report throttling as throttling — it answers with
`possibly delisted; no price data found`, for AAPL, MSFT and NVDA alike. One ticker of
two hundred survived. `prices_and_actions` raised only when *nothing* came back, so a
single survivor was enough to proceed, and `build()` wrote parquet directly into the
destination — overwriting a known-good snapshot with a one-security one. The resulting
snapshot would have built an index that looked entirely plausible.

Two failures, and the second is the worse one. A pipeline that loses today's data is
annoying; a pipeline that destroys yesterday's while doing so is dangerous.

**Decision.** A coverage floor (`min_price_coverage`, default 0.80) that refuses to write
below it, and staging-plus-rename so the destination is only touched once a complete
snapshot exists. Prices are cached per ticker, so a throttled run resumes instead of
restarting, and the error message says what the failure actually is rather than repeating
the vendor's claim that the S&P 500 has been delisted.

**Alternatives rejected.** *Trusting the vendor's error* — "possibly delisted" is
indistinguishable from a genuine delisting, which is precisely the ambiguity
`ProviderUnavailableError` was written to reject in `data/vendors.py`; accepting it here
would have contradicted the module's own rule. *Retry alone, with no floor* — retries help
a transient blip and do nothing for a sustained rate limit, and the run still ends by
writing whatever it managed to get. *Warning loudly and writing anyway* — the whole
argument for a publication gate is that plausible-looking wrong output is the expensive
failure; a warning in a log is not a control.

**Consequences.** A cold fetch is slower: per-ticker requests replace batches of forty,
because a batch that trips the limiter loses all forty results and caches none. The second
run is far cheaper, since every success is on disk. The floor is a judgement — 80% admits
the handful of names that legitimately have no history while rejecting a throttled run —
and it is in the config so a caller who genuinely wants a sparse universe can say so
explicitly rather than by accident.



---

## D-020 — The factsheet's disclosure is derived from the universe, not typed into it
**Date:** 2026-08-13 · **Module:** M13 (reporting) · **Status:** accepted

**Context.** `miniftse factsheet` built from the generator and nothing else, so its
Important Information section could state flatly that the index was computed on
**simulated market data**. Adding `--universe` — the obvious companion to
`build-index --universe`, and the thing that lets a real build produce a client-facing
document at all — made that sentence capable of being false on the one page in the
repository written in a client's register. A hardcoded caveat is safe only while the
input it describes cannot change.

**Decision.** Derive the section from `result.universe.summary()`. Absent a `provenance`
key the wording is unchanged, which is every existing caller. Present, the factsheet
names the source, prints the snapshot fingerprint, and reproduces `data.real.DEFECTS`
in full alongside the counts observed while fetching that particular snapshot.
`MaterialisedUniverse.summary()` gained `source` to support this; both keys are absent
from a generator-materialised snapshot, and that absence is the signal. The header
gained a **Series begins** clause, shown only when the first computed date differs from
`config.base_date` — on a fetched snapshot the build must start a liquidity window after
the data does, and quoting a base level on a date the series does not cover is a wrong
number rather than a presentational quibble.

**Alternatives rejected.** *A `--real` flag on the command* — puts the disclosure in the
caller's hands, which is precisely where a disclosure must not be; a wrong flag then
publishes a wrong disclaimer silently. *Summarising the defects in a sentence* — the
three that move numbers on the page (survivorship, no free float, split-adjusted prices)
are not interchangeable with each other and a reader cannot recover them from a
summary. *Refusing to render a factsheet from a real snapshot at all* — defensible, and
rejected because the document is the natural place to state what is wrong with the data,
not a reason to avoid producing it.

**Consequences.** The disclosure cannot drift from the data, because there is no copy of
it to drift. A real-data factsheet is roughly a page longer than the synthetic one and
most of that page is caveat, which is the honest ratio at this data quality. The default
output path becomes `artefacts/factsheet-<snapshot>.md` when `--universe` is passed, so
a real build cannot overwrite the committed synthetic factsheet by omission. Related:
the "review selected nothing" error now names the failing screen and its rejection
counts — the base-date trap above cost an afternoon to diagnose from the bare message,
and `test_a_review_that_selects_nothing_names_the_failing_rule` keeps it named.

---

## D-021 — The triage taxonomy's format check is scoped to its own files, not the whole tree
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** Task 1's brief (Step 7) specifies `uv run ruff format src tests` — unscoped
and mutating — as part of the lint/format verification for `src/miniftse/triage/` and
`tests/test_triage.py`. Running it as written touches every file under both trees, but
the task's own instructions permit creating exactly three new files and forbid modifying
anything under `src/miniftse/corpactions/`, `src/miniftse/calc/`, or any existing test
file. Running the unscoped command found 63 pre-existing files that `ruff format` would
reformat, none of which this task touched or has any reason to touch — formatting debt
that predates this task, confirmed by a clean `git status` on the branch before any of
this task's edits landed.

**Decision.** Run `ruff format` / `ruff format --check` scoped to the files this task
owns — `src/miniftse/triage/` and `tests/test_triage.py` — instead of the brief's
literal unscoped invocation. Both are clean under the scoped check. The repo's CI lint
job runs `uv run ruff format --check src tests` unscoped, so CI is red at HEAD on this
step independently of this task; that debt is real, pre-existing, and deliberately left
alone here rather than folded into a taxonomy-pinning commit.

**Alternatives rejected.** *Run the brief's literal unscoped command* — would have
silently reformatted 63 unrelated files as a side effect of a task scoped to three new
files, burying the actual diff for this change under whitespace noise in files this task
has no mandate to touch. *Reformat the whole tree deliberately, as a bonus cleanup* —
rejected because a repo-wide formatting pass is a real, reviewable change that belongs
in its own commit and its own task, not a side effect of pinning a label space.
*Skip the format check entirely* — rejected because "`ruff format --check src tests`
must pass" is one of this task's own global constraints, and the three new files still
needed verifying against it.

**Consequences.** CI's unscoped `ruff format --check src tests` remains red at HEAD,
unrelated to and unresolved by this task. A later task that wants a green CI lint step
either needs a dedicated formatting-cleanup commit across the 63 files or a change to
what CI checks; either is a call for whoever owns that decision, not a byproduct of
Task 1. The file count (63 unformatted, 26 already formatted, 89 total under
`src tests`) was confirmed by two independent `ruff format --check src tests` runs.

---

## D-022: Triage is graded in basis points, not classification accuracy
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

Accuracy weights a misread dividend amount the same as a return of capital booked as
an ordinary dividend. On a three-name fixture the second costs 67bp of index level and
the first costs under 5bp. Reporting one number that cannot tell them apart would make
the eval actively misleading.

**Rejected:** F1 over event types, which is the default for a classification task and
is what a reviewer will expect. It is retained as a secondary diagnostic (`same_type`)
because it localises *where* the error is, but it is not the headline.

---

## D-023 — `corpus.py` drops the brief's unused `field` import
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** Task 3's brief gives the complete text of `src/miniftse/triage/corpus.py`
verbatim, including `from dataclasses import dataclass, field`. Nothing in the module
uses `field` — every dataclass field is a plain annotation or a literal default, and
`Announcement`/`Provenance` need no `default_factory`. `uv run ruff check src tests`
flags it as F401 (imported but unused), and "`uv run ruff check src tests` must pass"
is one of this task's own global constraints.

**Decision.** Drop `field` from the import, keeping `dataclass`. The rest of the
brief's code is unchanged.

**Alternatives rejected.** *Keep the import as written, matching the brief exactly* —
would leave `ruff check` red on a file this task owns, and "follow the brief verbatim"
cannot mean shipping a known lint failure when the brief itself also states the lint
constraint. *Add a `# noqa: F401`* — suppresses a correct finding instead of fixing it,
and there is no forward-looking reason (no planned `field(default_factory=...)`) to
keep the import alive.

**Consequences.** None beyond the one-token diff; `Provenance`/`Announcement` behave
identically. Confirmed by `uv run ruff check src tests` and `uv run mypy src/miniftse`,
both clean after the change.

---

## D-024 — The corpus round-trips a label's whole dataclass, not `spec.required`
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** Code review of Task 3 caught a latent defect the tests did not exercise:
`Announcement.to_dict` serialised only `TAXONOMY[event_type].required` plus the five
`COMMON_FIELDS`, so any other field on a `CorporateAction` subclass was dropped on
write and silently replaced by its dataclass default on read. Confirmed against
`corpactions/events.py` as three concrete cases, two of them not metadata but inputs
to the grading engine's arithmetic: `Spinoff.spinco_enters_index` (default `True`)
directly flips `is_divisor_event`, so a labelled spin-off that does *not* enter the
index reads back as one that does. `Delisting.required` is `()`, so `final_price`
(default `0.0`) and `reason` (default `"DELISTED"`) were dropped on *every* delisting
label regardless of what was written — a real final price silently became a total
wipeout, feeding straight into `price_effect`. `CashDividend.currency` and
`withholding_rate` revert the same way, lower-stakes but still wrong. A stored label
could become a materially different event than the one a human or a vendor feed
actually labelled, with nothing raised — the round trip succeeded, it just returned
the wrong answer. `test_round_trips_a_labelled_announcement` did not catch this
because the only label it built (`CashDividend(amount=2.0)`) left every optional
field at its default, so dropping and re-defaulting them was indistinguishable from
preserving them.

**Decision.** `Announcement.to_dict` now serialises every field `dataclasses.fields()`
reports on the label instance, minus `COMMON_FIELDS` (captured separately, as before).
`build_event` (`taxonomy.py`) changes from `kwargs.update({f: payload[f] for f in
spec.required})` to `kwargs.update(payload)`, accepting whatever the caller supplies
beyond the required set, then applies `kwargs.update(spec.defaults)` last so
type-discriminating fields — `is_special` for `SPECIAL_DIVIDEND` — still come from the
event type rather than from `payload`, even though `payload` now happens to carry the
same value. `spec.required`'s missing-field check is untouched: a payload short a
required field still raises `TaxonomyError` before construction, exactly as
`test_rejects_a_missing_required_field` already pinned. Since `CorporateAction` is an
ABC and not itself `@dataclass`-decorated, `fields()` needed one explicit, commented
`cast(..., DataclassInstance)` in `corpus.py` to satisfy `mypy --strict` — every
concrete handler *is* a dataclass at runtime, the base class just doesn't say so
statically. Four new `TestCorpus` tests each build a label with a non-default optional
field (`currency`/`withholding_rate` on a `CashDividend`, `spinco_enters_index=False`
on a `Spinoff`, a non-zero `final_price`/non-default `reason` on a `Delisting`, and an
explicit `is_special=True` `SPECIAL_DIVIDEND`) and assert full dataclass equality after
the round trip, which the existing all-defaults test structurally could not do.

**Alternatives rejected.** *Keep serialising `spec.required` and add every currently-
known optional field to each `EventSpec` by hand* — fixes today's three cases but is
the same defect shape one field away: the next optional field anyone adds to a
`CorporateAction` subclass silently repeats this bug unless someone remembers to also
update the taxonomy entry, and nothing would catch the omission. *Pickle the label
instead of round-tripping through the taxonomy* — the fastest fix, and rejected for
the reason Task 3's brief gives for not doing this in the first place: the corpus is
meant to be diffable in `git diff`, and a labelling disagreement should show up as one
changed line of JSON, not an opaque binary blob. *Widen `spec.required` itself to mean
"every field"* — required is a real, load-bearing distinction (`build_event` must
still refuse to construct an event silently missing `amount`); conflating "must be
supplied" with "may be supplied" would have broken the missing-field validation this
task was explicitly told not to weaken.

**Consequences.** A `CorporateAction` label now round-trips through `write_jsonl`/
`read_jsonl` with full equality, verified for every event class this task could reach
without touching `corpactions/`. The corpus JSON payload is correspondingly wider —
every optional field appears on every row, not only the ones the taxonomy calls
required — which is a larger diff per label but the honest one: a labelling
disagreement on `currency` alone now actually shows up as a one-line diff, which is
what D-023's sibling design rationale already promised and this fix makes true.
`build_event` is very slightly more permissive about what `payload` may contain (extra
keys beyond `required` are no longer rejected or ignored, they are used), which is
exactly the behaviour the corpus needs and does not weaken any existing caller: every
current caller either passes exactly `required` (unaffected) or, after this fix, the
full field set of a real event (now handled correctly instead of silently truncated).

---

## D-025 — Unjoinable labels are dropped and counted, not matched approximately
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** Vendor corporate-action data (Task 4) carries no announcement text, only a
structured event and a date. SEC filings (`text.py`) carry text but no structured event.
Nothing links a label to a filing except the issuer and a date, so `join_labels_to_text`
must pair them heuristically: nearest filing by `filed` date, same `security_id`, within
`window_days`. A wrong join is worse than a missing one — it puts a correct label on the
wrong text, and every metric computed from that announcement (`verify.impact_error`
included) inherits the error silently, with no signal anywhere that it happened. A
missing join, by contrast, is visible: the label lands in `unjoined` and the corpus's
unjoined count goes up by one, where it can be inspected, reported, and reasoned about.

**Decision.** `join_labels_to_text` fails closed. A label with no `FilingDocument` of
the same `security_id` within `window_days` of its `announcement_date` is appended to
`unjoined` and never paired with the nearest available filing regardless of distance.
Candidates are filtered to the security first, then to the window, before the nearest
one is chosen — a same-day filing for a different issuer is never eligible, however
close the date. `unjoined` is returned alongside `joined`, not swallowed, so the count
is a published property of the corpus rather than an internal detail a caller has to
choose to expose. `TestTextJoin.test_drops_a_label_with_no_filing_in_the_window` and
`test_does_not_join_across_securities` each construct a case with exactly one plausible-
looking neighbour (a document 139 days away; a document on the exact date but the wrong
security) and assert it lands in `unjoined`, not `joined` — a regression that widened
the window or dropped the security filter would fail one of these visibly.

**Alternatives rejected.** *Widen the window until everything joins* — trades a visible
gap (the unjoined count) for an invisible error rate (mislabelled announcements with no
flag anywhere), which is the wrong trade in an eval set whose entire purpose is scoring
a model against ground truth. *Fall back to the nearest filing for the security when
none is inside the window* — same defect under a different name; "nearest, unbounded"
is still a guess dressed up as a match. *Silently drop unjoined labels instead of
returning them* — would hide exactly the number a reviewer most needs: how much of the
labelled ground truth the corpus could not attach text to, and therefore how biased the
resulting corpus is toward issuers/events with easy-to-find filings.

**Consequences.** The corpus is smaller than the label set whenever filings are sparse,
incomplete, or outside the fetch window, and that gap is a number callers must look at
rather than a coverage detail they can ignore. Downstream code that builds a corpus from
`join_labels_to_text` needs to decide what to do with `unjoined` — report it, retry with
a wider fetch, or accept the loss — rather than assuming every label became an
`Announcement`. The window itself (`window_days=5`, tunable by the caller) remains a
judgement call with no single correct value; too narrow drops real matches, too wide
raises the odds within the window that a wrong-but-plausible filing gets picked over the
true one. Splitting the join this way also kept `join_labels_to_text` pure and fully
testable with hand-built `FilingDocument`s — no network, no mocking `requests` to fake
one — while `fetch_filings`, the only network-touching function in `text.py`, stays
thin and untested by design, per this task's global constraint against network in tests.

---

## D-026 — A blank-after-strip filing is excluded from join candidacy, not offered as `best`
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** Code review of Task 5 caught a defect the tests did not exercise, in the
interaction between two pieces this task shipped together: `_strip_html` and
`join_labels_to_text`. `_strip_html` can legitimately reduce a filing body to nothing —
an all-`<script>`/`<style>` redirect or viewer page, an exhibit with no prose —
`_strip_html("<script>var x = 1;</script>")` returns `""`. `join_labels_to_text`'s
candidate filter (D-025) only checked security and window; a blank `FilingDocument`
that was otherwise the nearest match still became `best` and was passed straight into
`Announcement(text=best.text, ...)`. `Announcement.__post_init__` (`corpus.py`) rejects
blank text with `raise ValueError("announcement text is empty")` — uncaught anywhere in
`join_labels_to_text`, so it did not just drop the one label whose candidate was blank.
It escaped the function entirely, discarding `joined` and `unjoined` results already
computed for every other label processed in the same call. One malformed filing
anywhere in a corpus-building run would have destroyed every result computed before it
— a strictly worse failure than the "wrong join" D-025 already guards against, and one
that violates this module's own stated contract: a document that cannot plausibly serve
as an announcement's text should make the label land in `unjoined`, not take down the
batch.

**Decision.** `join_labels_to_text`'s candidate filter gains a second condition:
`document.text.strip()`, alongside the existing security and window checks. A blank or
whitespace-only document is now invisible to the join — never selected as `best`, and
therefore a label whose only in-window match is blank lands in `unjoined` exactly as if
no filing existed at all. The guard lives in `join_labels_to_text` itself, the pure and
tested function, not only in the network-touching `fetch_filings` — a check that lived
solely in the untested path would be unverifiable by this task's own test suite.
`fetch_filings` also gained a parallel skip (checking `_strip_html(body.text).strip()`
before appending a `FilingDocument`) so a blank filing is never even fetched into a
batch in the first place; this is additive, not a substitute for the
`join_labels_to_text` guard, since a caller can construct `FilingDocument`s directly
(as every test in `TestTextJoin` does) without ever going through `fetch_filings`.
Three new `TestTextJoin` tests pin this: a label whose sole candidate is blank lands in
`unjoined`; a blank candidate that is nearer by date still loses to a farther non-blank
one, proving exclusion rather than a mere tie-break loss; and a three-label batch with
one blank candidate returns complete, correct `joined`/`unjoined` results for the other
two and does not raise — the test that pins the batch-abort severity specifically. Two
of the three use whitespace-only text (`"   "`), not just `""`, since
`Announcement.__post_init__` rejects both via `.strip()` and a fix that checked
truthiness alone (`if not document.text`) would have passed the empty-string case while
still crashing on whitespace-only.

**Alternatives rejected.** *Catch the `ValueError` around the `Announcement(...)`
construction and route that label to `unjoined` on failure* — treats a predictable,
checkable condition (blank text) as an exceptional one, catches a broader exception
type than the one specific failure being guarded against, and leaves `best` chosen from
a candidate pool that never should have included the blank document — the exact
tie-break scenario in the second new test (a same-day blank filing beating a real one a
few days off) would still pick the blank filing as nearest and only avoid crashing by
accident of exception handling, not by correct candidate selection. *Guard only inside
`fetch_filings`, skipping blank bodies before they become `FilingDocument`s* — the
brief's own controller amendment already requires `join_labels_to_text` to be the
locus of judgement because it is the pure, tested function; a guard that lived only in
the untested network path would leave the pure join function still able to raise on a
hand-built blank `FilingDocument`, which is precisely how the reviewer reproduced this.
*Relax `Announcement.__post_init__` to accept blank text* — out of scope (that file is
explicitly not owned by this task) and wrong regardless: blank text is never a valid
announcement anywhere in the corpus, not just in this join path.

**Consequences.** `join_labels_to_text` can no longer raise on a blank or
whitespace-only `FilingDocument.text`, confirmed by re-running the three new tests
against the pre-fix candidate filter (security-and-window only) and observing all three
fail with the exact `ValueError` the reviewer described, then pass after restoring the
`document.text.strip()` condition. A corpus-building run over many labels is now
resilient to any single malformed filing — the worst case for one bad document is one
extra `unjoined` label, not a discarded batch. `fetch_filings` fetches marginally fewer
documents in the rare case of an all-markup body, which is the correct behaviour since
such a document was never going to survive `Announcement.__post_init__` regardless.

---

## D-027 — `extract_event`'s abstain path also catches `TypeError`, not just `KeyError`/`ValueError`/`TaxonomyError`
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** Task 6's brief writes `extract_event`'s validation `except` clause as
`(KeyError, ValueError, TaxonomyError)` — correct against Task 3's *original*
`build_event`, which built its `kwargs` from `{f: payload[f] for f in spec.required}`
and so could never pass an unrecognised key to a handler dataclass. D-024 changed that:
`build_event` now does `kwargs.update(payload)`, forwarding every payload key the
caller supplies, not only the required ones, so that a round-tripped corpus label keeps
optional fields like `Spinoff.spinco_enters_index`. That same permissiveness is a
liability at the other call site. `extract_event`'s system prompt names the permitted
`event_type` values but never enumerates each type's payload schema, so nothing stops a
model from hallucinating an extra key (`{"amount": 2.0, "confidence": 0.9}` for a
`CASH_DIVIDEND`) that is not in `CashDividend.__slots__`. `spec.required` is satisfied
(`amount` is present), so `build_event`'s own missing-field check never fires; the
unrecognised key survives all the way to `spec.handler(**kwargs)`, and a frozen,
`slots=True` dataclass constructor called with a keyword it does not declare raises
`TypeError: __init__() got an unexpected keyword argument '...'` — not a `ValueError`
subclass, so the brief's literal three-exception tuple does not catch it. Confirmed
directly: `build_event(EventType.CASH_DIVIDEND, common, {"amount": 2.0, "confidence":
0.9})` raises exactly that `TypeError`, uncaught, at the module's own top level. Left
uncaught inside `extract_event`, that `TypeError` propagates out of the function
entirely, which breaks this module's first and most important stated property —
malformed model output must abstain, never raise — for the specific case of a
hallucinated field name, arguably the single most likely way a real model output
diverges from the payload the taxonomy expects.

**Decision.** `extract_event`'s validation `except` clause is `(KeyError, TypeError,
ValueError, TaxonomyError)`, one member wider than the brief's literal text. `KeyError`
still covers a `payload`/`common` dict missing a key the code reads directly (e.g.
`parsed["ex_date"]`); `ValueError` still covers `dt.date.fromisoformat` on a malformed
date string and is also `TaxonomyError`'s base class, so it is kept for clarity even
though `TaxonomyError` alone would satisfy `except`'s subclass matching; `TypeError` is
the addition, and it is caught in the same tuple rather than a separate `except` block
because the response to all four is identical: abstain with `str(exc)` as the reason.
`test_an_unexpected_payload_field_abstains_rather_than_raising` pins this — a payload
with `amount` (satisfying `spec.required`) plus an unrecognised `confidence` key, which
reaches `spec.handler(**kwargs)` and only fails there, confirming the test exercises the
constructor path itself and is not short-circuited by the earlier missing-field check.

**Alternatives rejected.** *Follow the brief's exception tuple verbatim* — matches the
written task text, but ships a function whose own module docstring's property 1
("malformed output abstains; it never raises") is false for a specific, foreseeable
input shape, on the one code path in the repository where the input genuinely comes
from a language model. *Catch bare `Exception`* — would also satisfy the test, but
swallows genuine bugs (a typo in `_prompt`, a real `AttributeError` from code this
function doesn't own) behind the same "the model's fault, abstain" reasoning, making
`extract_event` a place where programming errors go to hide rather than fail loudly.
*Validate `payload`'s keys against the handler dataclass's declared fields inside
`build_event` and raise `TaxonomyError` instead of leaving the `TypeError` to the
constructor* — the more thorough fix, and rejected only because `taxonomy.py` is
outside this task's ownership (`taxonomy.py`, `corpus.py`, `labels.py`, `text.py` are
all "other triage/ modules" this task must not touch); catching the constructor's own
`TypeError` at the one call site that needs it is the change available without
reopening D-024's module.

**Consequences.** *(This paragraph originally claimed `extract_event` "now abstains
rather than raises for every failure mode the module's docstring claims to guard
against." That was false, and is corrected here rather than quietly reworded, per this
log's own D-014 precedent for a wrong claim caught after the fact. Code review of this
same task's commit found a second, unrelated raise path this decision does not touch:
`json.loads` happily parses valid JSON that is not an object — `[1, 2, 3]`, `"hello"`,
`42`, `null` — and `parsed.get("abstain")` ran unguarded outside every `try`/`except`
block, so each of those four raised an uncaught `AttributeError` before execution ever
reached the `TypeError`/`KeyError`/`ValueError`/`TaxonomyError` handling this decision
actually addresses. That gap was present in the brief's own Step 3 sample code, copied
verbatim by the first implementer and inherited unfixed through the writing of this
entry. See D-028 and `task-6-report.md`'s fix-round-1 section for that fix, and for a
related but distinct gap found in the same review pass: `build_event` accepted a
wrong-*typed* required value — `{"amount": "two dollars"}` — with no check at all,
since key presence was the only thing `spec.required` verified.)*
Within its own narrower scope, this decision does what it says: a hallucinated payload
key beyond `spec.required` now abstains rather than raises, alongside the
`KeyError`/`ValueError`/`TaxonomyError` failure modes the brief's original tuple already
caught. The cost is that `extract_event`'s `except` clause is one class wider than what
the brief specifies verbatim, which is exactly the kind of deviation this decision log
exists to record rather than land silently.

---

## D-028 — `build_event` validates a required field's runtime type, not just its presence
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** Code review of this task's commit found a second gap alongside the
`AttributeError` fixed in `extract.py` (see the corrected Consequences of D-027, above):
`TAXONOMY[event_type].required` and the `missing = [f for f in spec.required if f not in
payload]` check in `build_event` verify only that a required key is *present*, never
that its value is the right *type*. A Python dataclass does not validate types at
construction either - `CashDividend(amount="two dollars")` builds without complaint.
So `{"event_type": "CASH_DIVIDEND", "payload": {"amount": "two dollars"}}` from a model
built a real `CashDividend` with a string `amount`, `Extraction.abstained=False`, no
exception raised anywhere. That is precisely the failure `extract.py`'s own module
docstring names as the worst outcome - "a partially built event is the failure that
grades cleanly and is wrong" - and it happens silently downstream of every property
D-024 through D-027 were written to defend, because none of them check a value's type.
The reviewer's framing of where to fix it matters as much as the fix: property 3 of
`extract.py`'s docstring is "the taxonomy decides whether that is constructible," so a
type gate that lived only in `extract.py` would protect the one caller that happens to
be a language model and leave `build_event` - the function every caller, including
`corpus.Announcement.from_dict` reading a hand-edited corpus row, actually calls -
exactly as permissive as before. D-027's own "alternatives rejected" paragraph had ruled
out touching `taxonomy.py` for a related reason (an unrecognised payload *key*) on the
grounds that `taxonomy.py` was outside that task's ownership; this finding is different
in kind (a wrong-typed but correctly-*named* value) and the reviewer directing this fix
round explicitly authorised modifying `taxonomy.py` for it, which is the authorisation
D-027's paragraph did not have.

**Decision.** `build_event` gains `_check_required_types`, called after the existing
missing-field check and before `kwargs` is assembled: for each name in `spec.required`,
resolve the handler dataclass's own annotation for that field and confirm the payload
value's runtime type satisfies it, raising `TaxonomyError` on a mismatch - the same
exception type (and therefore the same caller-facing behaviour) as the missing-field
check right above it. Only `spec.required` fields are checked, matching the reviewer's
explicit scope; an optional field with a wrong-typed value (e.g. a non-string
`currency`) is unchanged by this decision.

Resolving "the handler dataclass's own annotation" is not `dataclasses.fields(handler)
[i].type` - `corpactions/events.py` has `from __future__ import annotations`, so every
class-level annotation there is stored as a string ("float", not `float`) under PEP 563.
`_field_type_hints` calls `typing.get_type_hints(handler)` instead, which evaluates
those deferred string annotations back into real type objects against the declaring
module's globals, and is cached per handler with `functools.cache` since the taxonomy is
fixed at import time and `build_event` may run once per announcement in a batch.

Two wrinkles in `_payload_value_matches`, both chosen for what a model's JSON payload
can actually contain rather than for type-theoretic completeness. First, `int` is
accepted wherever `float` is annotated: `json.loads('{"amount": 2}')` produces a Python
`int` for a whole-number literal, and a model asked for a $2 dividend's amount has no
reason to prefer "2.0" over "2" - rejecting a bare integer here would be over-tightening
against a shape models routinely produce, not a real defect. Second, `bool` is rejected
for every numeric field even though Python's `bool` is an `int` subclass and would
otherwise satisfy `isinstance(value, (int, float))` silently - `CashDividend(amount=
True)` would construct cleanly as a $1.00 dividend if that relaxation were allowed
through unchecked, which is the exact failure this decision exists to close, reopened
by the fix meant to close a different one. `bool` is checked first, before the
`int`-for-`float` relaxation applies, so it can never hide behind it.

**Alternatives rejected.** *Fix only the `AttributeError` in `extract.py` and leave the
wrong-type gap in `build_event`* - was the actual state after `extract.py`'s
non-dict-JSON guard alone; rejected because it leaves every other `build_event` caller,
not only a language model, able to construct a malformed event from a wrong-typed value,
and because the reviewer's finding was explicit that this belongs in the taxonomy, not
the one caller that happened to surface it. *Validate every dataclass field, not only
`spec.required`* - broader coverage, and rejected as over-reach relative to what was
actually shown to be broken: an optional field's default already comes from the type
itself in most cases (`spec.defaults`) or from the handler's own dataclass default, and
validating fields nobody demonstrated a problem with expands the change past its
evidence. *Coerce instead of reject (e.g. `float(value)` for a numeric field)* - would
silently turn `"2"` into `2.0` and mask exactly the ambiguity `TaxonomyError` exists to
surface; a coercion that happens to succeed on a string that merely looks numeric is a
guess, and this module's whole design is "raise rather than guess." *A bare
`isinstance(value, (int, float))` check with no `bool` carve-out* - simpler, and
rejected because it is a known Python foot-gun that would have passed
`test_rejects_a_bool_for_a_numeric_required_field` failing silently instead of raising:
`bool`'s `int`-subclass relationship is exactly the kind of "technically satisfies the
check" gap this decision was written to close, not reproduce one field over.

**Consequences.** `build_event` is now the single point that decides both whether a
required field is present and whether its value is usable, matching property 3 of
`extract.py`'s docstring ("the taxonomy decides whether that is constructible") for real
rather than only for key presence. Every `build_event` caller benefits, not only
`extract_event` - a hand-edited corpus JSONL row with a string `amount` now fails loudly
at `Announcement.from_dict` instead of building a silently-wrong label. The check is
deliberately narrow: it covers `spec.required` fields only, checks type and not value
range (a negative `amount` or a zero `ratio` still constructs), and its `int`-for-`float`
relaxation means a required `float` field does not distinguish "the model wrote a whole
number" from "the model wrote the wrong kind of whole number" - both build. Widening
either is a real question for whichever task next touches `taxonomy.py`, not one this
fix answers by omission.

The relaxation is also **one-directional, and that asymmetry is a real behaviour change
this entry did not originally state**: `int` satisfies an `int`-annotated field and a
`float`-annotated one, but a `float` supplied for an `int`-annotated required field -
`RightsIssue.new_shares` or `RightsIssue.per_held`, the only two in the taxonomy - is now
*rejected* where before this decision it built, since a bare `isinstance(value, int)` is
False for `1.0`. No live path produces one (the synthetic universe emits Python `int`s
for both, and `json.loads` produces an `int` for `1` and a `float` only for `1.0`), so
this was recorded as a deferred minor rather than fixed; it is stated here so the next
reader finds the asymmetry written down rather than by hitting it. *(The value-range gap
this entry's last paragraph left open - "a negative `amount` or a zero `ratio` still
constructs" - is now closed for ratios and entitlements by D-032, and the undeclared-key
gap D-027 left open by D-031.)*

---

## D-029 — Impact error grades the divisor as well as the level, and reports the worse of the two
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** D-022 fixed the metric as "apply both events to the same state and diff the
resulting index levels," taken verbatim from the spec's §2 formula. A whole-branch review
found that formula is blind to most of what it is supposed to grade, and the reason is
structural rather than a coding slip: `engine.apply_event` rebases the divisor for every
`event.is_divisor_event` precisely so that the level is *continuous* across the event
(`engine.py:131-140`). For a return of capital, a rights issue, a share-count change or
an ineligible spin-off, the level after the event therefore equals the level before it
and is completely independent of the event's parameters. Diffing levels alone scores a
**perfect 0.0000 bps** for extractions that are grossly wrong. Reproduced on the tests'
own three-name fixture:

| truth | predicted | old error_bps | truth divisor | predicted divisor |
|---|---|---|---|---|
| `Split(2.0)` | `SharesChange(1,000 -> 2,000)` | 0.0000 | 300 | 400 |
| `RightsIssue(1-for-4 @ 70)` | `RightsIssue(3-for-1 @ 10)` | 0.0000 | 317.5 | 330 |
| `ReturnOfCapital(2.00)` | `ReturnOfCapital(20.00)` | 0.0000 | 298 | 280 |

The first row is a misclassification between two stage-1 classes. `ImpactError` already
carried `predicted_divisor` and `truth_divisor`; the headline number simply ignored them.
This is also a spec deviation, not only a quality gap: §1.3 says the model "is graded on
whether the resulting divisor is right," and §4.4 says `verify` wraps `apply_event` *and*
`continuity_breaches` - the latter is not called anywhere in `triage/`.

**Decision.** `impact_error` computes two components and reports the worse:

    level_error_bps   = |predicted_level - truth_level| / |truth_level| x 10,000
    divisor_error_bps = |predicted_divisor / truth_divisor - 1|          x 10,000
    error_bps         = max(level_error_bps, divisor_error_bps)

Both components are exposed as their own fields on `ImpactError`, so a caller can see
which one dominated - a 67bp level error and a 67bp divisor error are the same
misclassification seen twice, and a 0bp level error with a 3,333bp divisor error is a
structural misbooking the level was never going to show. The existing field names
(`error_bps`, `predicted_level`, `truth_level`, `predicted_divisor`, `truth_divisor`,
`same_type`) all keep their meanings; the change is additive apart from `error_bps` now
being a maximum rather than the level term alone.

The canonical `test_return_of_capital_booked_as_a_dividend` figure is **unchanged at
67.114 bps**, and that is a check on the change rather than a coincidence: a
`CashDividend` is not a divisor event, so the truth divisor stays at 300 while the
predicted `ReturnOfCapital` rebases to 298, giving a divisor error of
`|298/300 - 1| x 10,000 = 66.667 bps` - real, but smaller than the 67.114 bps level
error, which therefore still sets the headline.

Computing `predicted_divisor / truth_divisor` inside `verify.py` is a **comparison of two
numbers the engine returned**, not index arithmetic: no level, market value or divisor is
recomputed here, and the constraint the module docstring inherits from
`test_desk_contains_no_index_arithmetic` is about who is allowed to *derive* a published
figure, not who is allowed to subtract two of them. The same argument already licensed
the level diff.

**Alternatives rejected.** *Sum or root-sum-square the two components* - rejected because
they are two views of one error, not two independent errors: a divisor event moves the
divisor exactly so that the level does not move, so adding them double-counts every
misclassification where both are non-zero and would silently inflate the flagship
67.114 bps figure to 133.8. *Report the divisor error only* - simpler, and wrong in the
other direction: the dividend-vs-return-of-capital case is the project's flagship example
and its level error is the larger and more meaningful of the two. *Call
`engine.continuity_breaches` as §4.4 literally specifies* - it answers a different
question. It scans one engine's audit trail for divisor events whose level moved when it
should not have, i.e. for defects in the *engine*; grading asks whether two *different*
events produce the same divisor, and both sides here are individually continuous by
construction. Wiring it in would report zero breaches on every row in the table above.
*Fold the split-ratio case in by comparing constituent share counts* - rejected as
inventing a number: see the consequences below.

**Consequences.** Three of the four grossly-wrong extractions the reviewer reproduced now
score 3,333.33, 393.70 and 604.03 bps instead of 0.0000. The fourth does not, and cannot:
`Split(ratio=2.0)` versus `Split(ratio=10.0)` still scores exactly 0.0000, in both
components, because `Split` is market-value-invariant by construction (`_apply_split`
raises if it is not) and is not a divisor event - the two predictions leave *identical*
levels and *identical* divisors. The index impact of a wrong split ratio genuinely is
zero on the day; what the error corrupts is the price/share decomposition, which is not
an index quantity, and manufacturing a bps figure for it would mean deriving one outside
the engine. The honest handling is a signal rather than a number, so `ImpactError` gains
`identical_events`: 0.00 bps with `identical_events=False` reads as "no index impact",
not "correct answer", and `test_two_splits_with_different_ratios_are_genuinely_zero_impact`
pins both the zero and the flag so the limitation is enforced rather than remembered.
Splits therefore remain outside the primary metric even though `SPLIT`/`REVERSE_SPLIT`
are `in_scope_stage=1`; the spec's own secondary metric ("parameter accuracy conditional
on correct type", §2) is where a split ratio is scored, and stage 2 must report it rather
than leaning on the bps headline alone. Getting a split's *class* wrong is caught, at
3,333 bps, and that is the failure that moves a published level.

---

## D-030 — An event on a security the index does not hold is ungraded, never 0.0 bps
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** `apply_event` returns the state untouched, with a `"skipped: not a
constituent"` note, when `event.security_id` is absent from `state.constituents`
(`engine.py:98-106`) - correct engine behaviour, since events fire for securities outside
an index constantly. `impact_error` never checked membership, so both sides came back
with the starting level and the starting divisor and the pair scored **0.0000 bps**:
structurally indistinguishable from a flawless prediction, and produced by a comparison
in which neither event was applied to anything. This is on the live path, not a
hypothetical: `harvest_labels` sets `security_id` from vendor data (`"AAPL"`), nothing in
`triage/` builds an `IndexState` containing those identifiers, and the obvious wiring -
harvest free labels, grade them against the `make_state()` fixture - yields an all-zeros
scoreboard that reads as a perfect model. The half-off-index case is worse than the
symmetric one, because it produces a plausible non-zero number (67.114 bps, from the
truth side alone moving) rather than an obviously suspicious zero.

**Decision.** `impact_error` checks both events' `security_id` against
`state.constituents` before applying anything, and returns a separate `Ungraded` result -
`reason` plus `detail` - if either is absent. `Ungraded` is deliberately **not** an
`ImpactError` with `error_bps=0.0` and deliberately not a subclass of one: it has no
`error_bps` attribute at all, so a caller that sums or percentiles it into a scoreboard
raises `AttributeError` immediately instead of averaging in a flattering zero. The return
type becomes `ImpactError | Ungraded`, which mypy forces every `src/` call site to narrow.
Both sides are checked, not only the prediction: a corpus label on a name the state does
not hold is exactly as ungradable, and scoring it zero would credit the model for a
defect in the harness.

**Alternatives rejected.** *Raise instead* - the reviewer's other option, and rejected
because it collides with the exception boundary D-032 adds for the same function: a
grading run over a corpus must not die on one row, and a raise that every caller has to
wrap is a boundary in name only. *Return `ImpactError` with `error_bps = float("nan")`* -
propagates rather than stops, which is the appeal, but `nan` compares false against every
threshold, so a "count exceeding 1bp" tally (spec §2) silently omits it and the row
disappears from the scoreboard exactly as a zero would. *Build the state from the events'
own securities inside `impact_error`* - would make every pair gradable, and is precisely
the index arithmetic this module is forbidden to do; it would also have to invent the
constituent's weight, which `test_the_misclassification_penalty_is_weight_dependent_but_
its_ratio_is_not` shows is most of the answer. *Check only the predicted side* - cheaper,
and leaves the harness defect (a corpus label off-index) scoring zero, which is the
direction that flatters the model.

**Consequences.** Grading a harvested corpus against a state that does not contain its
securities now fails visibly, one `Ungraded` per pair, rather than reporting a perfect
score. The eval harness that consumes this (stage 2, `evals/`) must report the ungraded
count alongside the bps distribution - the same fail-closed-and-count contract D-025 set
for the text join and D-031 sets for the label harvest - because a scoreboard over 40% of
a corpus is a different claim from a scoreboard over all of it. Callers now have a union
to narrow, which is friction, and the friction is the point: the type is what stops the
next caller writing `sum(r.error_bps for r in results)`.

---

## D-031 — An undeclared payload key is dropped, not fatal, and the harvest reports what it dropped
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** Two findings with one cause, recorded together because fixing either alone
is worse than fixing neither. `build_event` did `kwargs.update(payload)` and then
`spec.handler(**kwargs)`, so any payload key the handler dataclass does not declare
raised `TypeError` from the constructor. The repo's own `SyntheticUniverse` emits exactly
such keys: `gross_amount` alongside `amount` on every ordinary and special dividend
(`synthetic.py:530,542`) and `terp` alongside the entitlement on every rights issue
(`synthetic.py:596`). `labels.py` caught `(TaxonomyError, ValueError)`, which does not
include `TypeError`, so the first affected row propagated out of `harvest_labels` and
**aborted the entire harvest** - the third instance on this branch of the
one-bad-row-kills-the-batch shape D-026 and D-027 each fixed once. Measured on the
shipped synthetic universe: 12,036 of 12,253 corp-action rows unbuildable, 98.2%, of
which 11,834 are the cash dividends this module exists to collect for free. D-027 had
already flagged the over-wide-payload gap as open "for whichever task next touches
`taxonomy.py`."

The obvious repair - catch `TypeError` in `labels.py` and skip the row - converts a crash
into silence and keeps the 98.2%. `labels.py` exists because "vendor data labels cash
dividends for nothing"; a fix that labels none of them is not a fix. And nothing in
`harvest_labels`'s signature would have said so: it returned a bare `list`,
`continue`-ing past every failure, while `join_labels_to_text` returns
`(joined, unjoined)` (D-025, titled "dropped **and counted**") and `extract_event`
returns `abstained` and a `reason`. It was the one fail-closed stage in the pipeline
whose losses were invisible, and that is the difference between knowing the corpus is 98%
smaller than intended and never finding out.

**Decision.** Two changes, both in the direction of "lose less, and say what you lost".

`build_event` filters `payload` to the handler dataclass's declared field names
(`_declared_fields`, `dataclasses.fields` cached per handler) before `kwargs.update`. An
undeclared key describes nothing this taxonomy models, so dropping it loses nothing,
whereas rejecting the row loses the label. Everything in `spec.required` is still checked
for presence, type (D-028) and range (D-032), which is where the information that
actually decides the divisor lives. `payload` is also guarded to be a mapping: it is
annotated `dict[str, Any]`, which checks nothing at runtime, and the caller nearest a
live model passes `parsed.get("payload", {})` straight through - filtering a JSON list
would raise `AttributeError`, which nothing catches, where a `TaxonomyError` is what
every caller already handles.

`harvest_labels` returns `(labels, skipped)`, with `SkippedRow` carrying the event id,
security, raw event type, a short grouping `reason` (`unknown_event_type` or
`unbuildable`) and the exception message. `skip_counts` summarises them as
`reason:event_type -> count`, because the useful question when a harvest comes back small
is never "how many" alone - it is which classes, which tells you whether the corpus lost
its long tail or lost its bulk. The `except (TaxonomyError, ValueError)` is tidied to
`except (TypeError, ValueError)`: `TaxonomyError` subclasses `ValueError`, so naming both
was redundant, while `TypeError` genuinely is not covered and is what aborted the batch.

**Alternatives rejected.** *Catch `TypeError` in `labels.py` and skip the row* - the
measured cost is 1,778 of 1,815 rows on the reviewer's fixture and 12,036 of 12,253 on
the default one; a free-label module that labels 1.8% of the free labels has no reason to
exist. *Forward only `spec.required` to the handler* - would also stop the `TypeError`,
and would silently drop every optional field, reintroducing verbatim the corpus-mutation
defect D-024 was written to close (`Spinoff.spinco_enters_index` reverting to `True` and
flipping `is_divisor_event`). `test_a_declared_optional_field_still_survives_the_filter`
exists to fail if anyone tries it. *Add `gross_amount` and `terp` to the dataclasses* -
touches `corpactions/`, which is fixed by the spec's own non-goals ("not a rewrite of
`corpactions/`"), and would encode one vendor's field names into the event model; the
next provider brings different ones. *Fix `synthetic.py` to stop emitting the keys* -
same objection from the other end, and it treats a taxonomy that cannot tolerate an
unfamiliar field as though the data were at fault. *Return counts only, not the rows* -
loses the event ids, which is what makes a skipped row investigable rather than merely
tallied.

**Consequences.** `harvest_labels` recovers **12,253 of 12,253** rows on the shipped
synthetic universe, up from an aborted batch and from 217 under a catch-and-skip repair.
Its signature changed, so every caller unpacks a tuple; the only callers today are tests.
An undeclared payload key is now silent, which is a genuine loss of signal: a model that
hallucinates a `confidence` field is no longer detected here, where D-027 made it
abstain. That trade is deliberate and asymmetric on purpose - the cost of tolerating a
stray key is one unnoticed hallucination in a field the grader never reads, and the cost
of rejecting one is the corpus. `test_an_unexpected_payload_field_is_ignored_rather_than_fatal`
replaces the D-027-era test that asserted the opposite, and says so in its docstring
rather than being quietly deleted. D-027's own decision - widening `extract_event`'s
`except` to include `TypeError` - is now doing less work than it was, but is **not**
redundant: `dt.date.fromisoformat(20240610)` raises `TypeError` for a non-string date,
and that runs before `build_event` is called at all, so removing the clause would reopen
a crash path on a shape a model produces easily.

---

## D-032 — Ratios and entitlements are range-checked at construction, and the grader has an exception boundary anyway
**Date:** 2026-08-14 · **Module:** M14 (triage) · **Status:** accepted

**Context.** D-028's closing paragraph named the gap it left open: the type check "checks
type and not value range (a negative `amount` or a zero `ratio` still constructs)". A
whole-branch review then found the third occurrence of the defect already fixed twice on
this branch - one malformed row propagating an exception out of a batch function and
discarding every result computed before it. All three of these escaped `impact_error`
uncaught:

* `Split(ratio=0.0)` -> `ZeroDivisionError` at `events.py:228` (`1.0 / self.ratio`)
* `Spinoff(shares_per_parent_share=0.0)` -> `ValueError` at `events.py:428`
* `RightsIssue(per_held=0)` -> `ValueError` at `events.py:320`

and `build_event` type-checked all three fields without range-checking them, so
`extract_event` returned `Split(ratio=0.0)` as a **non-abstained** extraction: `0.0` is a
perfectly valid `float`. A negative ratio is quieter still - `Split(ratio=-2.0)`
constructs, applies, and *passes* the engine's own market-value-invariance assertion,
because price and share count both flip sign and their product is unchanged, leaving a
negative price sitting in the index.

**Decision.** Both halves, because they fail differently. Validation stops the common
case at the point where the taxonomy already decides constructibility; the boundary stops
the case nobody has thought of yet.

`EventSpec` gains `positive: tuple[str, ...]`, listing required fields that must be
strictly greater than zero, and `build_event` enforces it after the type check:
`ratio` for `SPLIT`/`REVERSE_SPLIT`/`BONUS_ISSUE`, `new_shares` and `per_held` for
`RIGHTS_ISSUE`, `shares_per_parent_share` for `SPINOFF`. These are ratios and per-share
entitlements, where positivity is a fact about the event class rather than a judgement
about the data - which is what makes them safe to settle once in the taxonomy for every
caller, rather than per-caller. `test_every_positive_field_is_also_a_required_field`
pins the invariant `_check_positive` relies on to index the payload directly.

`impact_error` wraps both engine applications in
`except (ArithmeticError, TypeError, ValueError, CorporateActionError)` and returns the
`Ungraded` result D-030 introduced. The tuple is deliberate rather than bare:
`ArithmeticError` covers `ZeroDivisionError` from a collapsed divisor as well as from a
zero ratio, `TypeError` covers a wrong-typed field that reached an event without passing
through `build_event`, `ValueError` covers `events.py`'s own guards, and
`CorporateActionError` covers the engine's (an unhandled event class, a split that moved
market value). A bare `except Exception` would also swallow programming errors in the
harness itself and make a broken caller look like a corpus of bad rows.

**Alternatives rejected.** *Range-check in `build_event` only* - leaves the boundary
open, and a prediction can reach `impact_error` without passing through `build_event` at
all (a hand-constructed event, a corpus row written before this check existed, a future
extractor); the tests construct all three bad events directly for exactly that reason.
*Add the boundary only* - converts the crash into an `Ungraded` row, which is right, but
lets `extract_event` keep returning `Split(ratio=0.0)` as a confident non-abstention,
which is the failure `extract.py`'s docstring calls the worst one. *Range-check every
numeric field* - over-reach on the same argument D-028 rejected it with: a zero
`amount` is a real if unusual dividend, a zero `final_price` is the documented default
for a delisting, and a zero `subscription_price` still computes a valid TERP. Only the
fields that divide are constrained. *Constrain `RightsIssue.cum_price` too* -
`price_effect` divides by it, but the engine never calls `RightsIssue.price_effect`
(`_apply_split` is its only caller; `_apply_rights` uses `terp` directly), so the
division is unreachable on the live path and constraining it would be guessing at a
defect rather than closing one. Noted here so the next reader does not have to redo the
grep.

**Consequences.** `extract_event` abstains on a zero or negative ratio instead of
returning a confident, poisoned event, and a grading run over a corpus survives a bad row
with one `Ungraded` result rather than a traceback and nothing at all -
`test_a_bad_prediction_does_not_stop_the_next_pair_grading` pins that specifically, since
a boundary that is never exercised across a batch is a boundary nobody has tested. The
range check is narrow by construction and inherits D-028's caveat unchanged: a required
field not in `spec.positive` still accepts any value its type allows. `impact_error` now
converts an engine exception into a returned value, which means a genuine engine bug
shows up as an ungraded row rather than a crash; the `detail` field carries the exception
class and message so the row is still investigable, and the stage-2 harness must surface
`Ungraded` counts by reason for that to be worth anything.
