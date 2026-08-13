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
