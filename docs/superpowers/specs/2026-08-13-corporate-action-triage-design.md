# Corporate Action Triage — design

**Date:** 2026-08-13 · **Status:** approved, not started

An evaluated AI system that reads real corporate action announcements, produces the
structured index event, and is graded by running that event through the existing
calculation engine — **measuring error in basis points of index impact rather than in
classification accuracy.**

Built for the Manager, Quant Researcher/Developer role in FTSE Russell Index Research &
Design (London).

---

## 1. Why this, and what it replaces

The application is already submitted; a call could come in week one or week eight. That
makes **completeness at every point** the governing constraint. An insider familiar with
the role reports the assessment focuses on **Python knowledge** and **AI fluency**.

### 1.1 The premise is stated by the code itself

`corpactions/events.py` opens with:

> *"The taxonomy matters more than the code: getting an event into the right class is most
> of the work, and the classic incidents are misclassifications — a return of capital
> booked as an ordinary dividend, a scheme of arrangement booked as a delisting."*

That is the task. It is genuinely hard, it is where real index incidents come from, and —
uniquely — **the deterministic component of the verifier already exists**:
`apply_event(event, state)` computes the divisor adjustment and returns the resulting
state, and `continuity_breaches(tolerance_bps)` already reasons about divisor changes in
basis-point terms.

The impact-error diff itself (§4.4) is new work. It is small — apply two events to the
same state and subtract the resulting levels — but it is not free, and the estimate in §5
assumes it is built at stage 1.

### 1.2 Rejected alternatives

| Rejected | Why |
|---|---|
| **Methodology Change Impact Studio** — codify Russell US rules, diff rulesets through the engine, reproduce the published semi-annual reconstitution consultation | Right target for a *research* assessment, wrong one here. ~200 hours, nothing shippable before week 3. Incompatible with "could be called any week." |
| **Membership reproduction study** — codified eligibility screens vs actual IWB/IWM holdings, with a residual catalogue | Strongest single finding available, but heavy on index domain and light on AI. Wrong axis. |
| **Ground Rules RAG assistant** — citation-grounded Q&A over published methodology, with an eval harness | Rejected after scoping. It is a commoditised pattern, ~70% already exists in `agents/`, and critically it is **weaker evidence than the candidate's own CV** — a portfolio RAG demo undercuts a claim to running a 100+ application AI platform at 500K users. Its eval harness was the only differentiating part, and that idea survives here in stronger form. |

Both index-research alternatives remain viable follow-ups if the process runs long.

### 1.3 What makes this different from a portfolio AI project

- It is a **decision** task, not a retrieval task — where AI actually earns money in index
  operations, and squarely the "investigate and resolve complex data and operational
  issues" line in the job description.
- It has a **deterministic downstream verifier.** The model is not graded on whether its
  answer reads plausibly. It is graded on whether the resulting divisor is right.
- Error is denominated in **basis points of index impact** — an AI evaluation expressed as
  a business risk number.

---

## 2. The metric — the intellectual core

**Classification accuracy is the wrong metric and using it would be the obvious mistake.**

A wrong ratio on a small cash dividend is nearly free. A spin-off misclassified as a
special dividend breaks divisor continuity and moves the published level. Accuracy scores
these identically. Basis points do not.

**Primary metric: index impact error.** For each announcement, apply the model's extracted
event and the hand-labelled true event to the same `IndexState`, and record the absolute
difference in resulting index level, in bps.

```
impact_error_bps = |level(apply(predicted, state)) - level(apply(truth, state))|
```

Reported as: median, 95th percentile, worst case, and the count exceeding a 1bp
publication-relevant threshold.

**Secondary metrics:** type accuracy (still reported — it is diagnostic), parameter
accuracy conditional on correct type, abstention correctness, malformed-output rate.

This inverts the usual relationship: the eval is not a proxy for business value, it *is*
the business value, in the units the business uses.

---

## 3. Goal and non-goals

**Goal.** Given real corporate action announcement text, produce the structured
`CorporateAction`, abstain when genuinely ambiguous, and publish an evaluation denominated
in index impact — including the failures.

**Non-goals:**

- Not an autonomous pipeline. Output is a *proposal for review*, mirroring the existing
  `triage.py` stance that a human approves anything touching published data.
- Not a rewrite of `corpactions/`. The engine and taxonomy are fixed; this feeds them.
- Not fine-tuning. Structured output, prompting, evaluation.
- Not full coverage of all 16 event types at stage 2 — see §5.
- No new index maths. Any figure the system reports comes from the engine.

---

## 4. Architecture

Four new units plus the existing engine. Each has one job and a defined interface.

### 4.1 `announcements/` — the labelled corpus (new)

Real corporate action text with provenance (source URL, filing date, retrieval date,
SHA-256) and a hand-verified `CorporateAction` label.

**Sources, in ascending order of labelling cost:**

| Source | Gives | Labelling |
|---|---|---|
| yfinance actions series | splits, cash dividends with ratios/amounts | **free labels** — structured data alongside the text |
| SEC EDGAR 8-K (Items 2.01, 8.01) | mergers, spin-offs, dispositions | text; hand-labelled |
| Exchange / issuer press releases | rights issues, tender offers, schemes | text; hand-labelled |

The free-label tier matters: it means the eval set is not gated on 80 hours of manual
work. Easy classes are labelled automatically and in volume; manual effort concentrates on
the hard classes where it is actually informative.

**Class imbalance is inherent** — cash dividends vastly outnumber spin-offs. The eval set
is **stratified deliberately**, and the stratification is published, because an unstratified
set would score 97% by predicting CASH_DIVIDEND and would be worthless.

### 4.2 `taxonomy/` — pinning the label space (new, small)

16 `EventType` values map to ~10 concrete handler classes; several share behaviour. Before
any extraction work, produce an explicit table: every `EventType` → its handler class →
its required parameters → whether it is in scope for stage 2.

Small, unglamorous, and blocks everything else. A model cannot be graded against an
ambiguous label space.

### 4.3 `extract/` — announcement text → `CorporateAction` (new)

Anthropic SDK with structured output against a JSON Schema derived from the event
dataclasses. Returns a `CorporateAction` instance or an explicit abstention with a reason.

**Abstention is a first-class outcome.** Genuinely ambiguous announcements exist — a
scheme of arrangement that could be a merger or a delisting depending on terms not in the
text. Abstaining is correct there, and is measured, exactly as `agents/evals.py` already
does.

Malformed structured output is a distinct failure class from a wrong-but-valid event and
is counted separately.

### 4.4 `verify/` — the grader (new)

Applies predicted and true events to an identical `IndexState`, diffs the resulting level,
returns impact in bps. Wraps `engine.apply_event` and `engine.continuity_breaches` — it
computes nothing itself, which keeps a single source of truth for index arithmetic and
matches the constraint `test_desk_contains_no_index_arithmetic` already enforces on the
desk.

### 4.5 `evals/` — harness and scoreboard (extends `agents/evals.py`)

Existing harness shape reused. New: bps-denominated reporting, stratified sampling, and
per-class breakdown.

**Dependency graph:** `taxonomy` → `announcements` → `extract` → `verify` → `evals`. Linear;
each stage testable with the next stubbed.

---

## 5. Staging — the ratchet

Every stage ends with something complete and sendable. Stage N+1 does not begin until
stage N is finished and written up.

### Stage 0 — this weekend (~5 hours). Independent of this project.

Without it, an early call finds nothing to show.

1. **Push `miniftse` to GitHub, public.** `git remote -v` is currently empty.
2. **Make CI green.** `HANDOVER.md` records `make ci` failing at typecheck — 18
   `unused-ignore` errors from pandas-stubs newer than the ignore comments. Pre-existing
   and confirmed on a pristine HEAD. Pin the stubs in `uv.lock`.
3. **Deploy the ops desk to Cloud Run.** Runbook already in `desk/README.md`.
4. **Rewrite the README's opening paragraph** to lead with what the platform does.
5. **Correct stale figures in `docs/ai_development_retrospective.md`** — it claims "~20,000
   lines across 68 modules with 89 tests" against a repo now at 79 source files and 236
   `def test_` functions. Re-measure rather than copying these numbers.

### Stage 1 — week 1. Taxonomy table and the free-label corpus.

`taxonomy/` complete. Corpus populated with auto-labelled splits and dividends at volume.
Extraction running end to end on the easy classes.

*Sendable:* "structured extraction over N real announcements, verified against the engine."

### Stage 2 — week 2. **The bps-denominated eval. This is the artefact.**

Stratified eval set, impact error reported in basis points, failures published and
characterised. If only one stage ships, this is the one.

### Stage 3 — week 3. The hard classes.

Spin-offs, rights issues, mergers — hand-labelled. Abstention behaviour measured. This is
where the interesting failures live and where the write-up gets its content.

### Stage 4 — week 4+. Presentation.

A desk screen, and the write-up as a standalone note. Pure upside.

**Minimum viable artefact: end of week 2.**

---

## 6. Failure modes

| Risk | Handling |
|---|---|
| Well-formed but wrong event | Caught by bps diff — the entire point of the design |
| Malformed structured output | Schema validation; counted as its own class, never silently retried into a pass |
| Genuinely ambiguous announcement | Abstention allowed and measured; ambiguous cases kept in the set rather than removed |
| Class imbalance flatters the score | Stratified eval set, stratification published, per-class breakdown reported |
| Model non-determinism | Temperature fixed, model version pinned and recorded per run |
| Inference cost | Responses cached on announcement hash; CI stays offline via `OfflineLlm` |
| Announcement text is copyrighted | Store URL + hash + extracted fields, not full text, for anything beyond short quotation |
| Called before week 2 | Stage 0 completes this weekend and is the insurance policy |

---

## 7. Deliverables

1. **The eval write-up** — why bps rather than accuracy, the stratification, the
   scoreboard, and the failure analysis. The piece most likely to be read closely.
2. **Public repo**, green CI, README leading with the system.
3. **Live URL** — desk screen, if stage 4 is reached.
4. **The AI-assisted development note** — specific about where AI assistance introduced
   plausible-but-wrong domain logic and what caught it. Directly answers the job
   description's AI-assisted-development line, which most candidates cannot address
   concretely.

**Positioning.** Independent research, public sources cited, no FTSE Russell branding or
implied affiliation.

---

## 8. Conventions

Existing repo standards: Python 3.12/3.13, `uv`, `ruff`, `mypy --strict` on the core,
`pytest` with property tests where the invariant is expressible, every judgement call
recorded in `DECISIONS.md` with the alternative rejected.

---

## 9. Open questions

1. **How many hard-class labels are enough?** Spin-offs and rights issues are rare and
   expensive to label. Decide at stage 3 with the stage-2 error distribution in hand — if
   impact error concentrates in one class, label that class deeply rather than all classes
   evenly.
2. **Whether to include a retrieval step.** Some announcements reference terms held in a
   separate exhibit. Adding retrieval is justified only if stage 3 shows missing-context
   errors as a material share of impact error.
