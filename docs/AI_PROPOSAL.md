# Three AI tools that would pay for themselves in Index Research & Design

**Internal proposal · miniFTSE Index Services · August 2026**

---

## Summary

Three tools, in the order I would build them. Each is prototyped in this repository, each
has a measured result rather than a claim, and each is scoped so that a wrong answer is
inconvenient rather than dangerous.

| # | Tool | Effort | Annual time saved | Principal risk |
|---|---|---|---|---|
| 1 | Methodology assistant with citations | 6–8 weeks | ~450 analyst hours | Confident wrong answers about published rules |
| 2 | Data-quality triage agent | 4–6 weeks | ~200 hours, faster incident response | Misdiagnosis leading to a wrong correction |
| 3 | Client-response drafter | 3–4 weeks | ~150 hours | An invented number reaching a client |

The three risks are different in kind, and each tool is built around its own. That is
the substance of the proposal — the model choice is nearly irrelevant by comparison.

**One principle underpins all three:** *the model never produces a number.* Numbers come
from code; the model arranges words around values it is handed. This is enforced
mechanically, not by prompting.

---

## 1. Methodology assistant

### The problem

The Ground Rules, policy documents and market notices run to several hundred pages.
Research, Product, Sales and Client Services all query them constantly. Most questions
are answerable from a single paragraph, and the person asking either interrupts a
colleague who knows, or spends twenty minutes searching PDFs, or — worst and most common
— answers from memory of a rule that has since changed.

### What was built

Retrieval-augmented question answering with page-level citations, in
`src/miniftse/agents/rag.py`.

Four design decisions carry the value, and none is about the model:

**Section-aware chunking.** Ground Rules are hierarchical legal-ish text where the
heading carries essential scope. A chunk reading "the minimum is 5%" is worthless without
"§5.1 Developed Markets", because the emerging-market threshold is 15% and both sentences
are true. A fixed-window splitter separates them routinely.

**A scope gate before retrieval.** This is the finding I did not expect. Retrieval score
*cannot* detect an out-of-scope question, because an out-of-scope question in this domain
shares all its vocabulary with the corpus. "What is the index level today?" retrieves the
calculation section with a high score and yields a confident, correctly-cited, useless
answer. Only the *kind* of question distinguishes it, so four question types — live
values, investment advice, other providers' rules, and holdings — are intercepted by rule
before retrieval runs.

**Superseded documents down-ranked and labelled.** Answering from last year's rules is
worse than not answering.

**Abstention measured as a first-class metric.** A system that always answers is a system
that guesses.

### Measured result

41 graded questions, including deliberately unanswerable ones:

| Metric | Result |
|---|---|
| Answer accuracy | **88%** |
| Citation precision | **100%** |
| Abstention accuracy | **98%** |
| Hallucinated numbers | **0%** |

Run with `make evals`. The eval suite is a CI gate, so the numbers in this document
cannot drift away from reality without the build failing.

### Documented failure modes

Five cases fail, and they are worth more than the 36 that pass:

1. **`A05` — a plausible rule that does not exist.** Asked about a carbon reduction
   requirement, the assistant answers from the adjacent section on index variants
   instead of abstaining. This is the hardest class of question and the most dangerous:
   the corpus contains *related* material, so retrieval succeeds and the scope gate has
   nothing to catch. Mitigation would be a "does the retrieved text actually address
   this?" check, which is a second model call and a second thing to evaluate.
2. **Three `review` cases** where the answer is in a markdown table. Table extraction
   improved substantially once table rows were treated as separate units, but remains
   the weakest part of retrieval, and real Ground Rules are far more table-heavy than
   this one.
3. **`C02`** where the required word appears inside markdown emphasis.

### Cost and effort

Six to eight weeks for one engineer to production standard: ingestion for real PDFs,
access control, an eval set built with Research, and a feedback loop. Running cost is
low — a few hundred queries a day at a few thousand tokens each.

Time saved: if 25 people each lose 30 minutes a week to methodology lookups, that is
roughly 650 hours a year. At 88% accuracy with reliable abstention, capturing 70% of it
is ~450 hours.

**The honest caveat:** the value is in the retrieval and the guardrails, not the model.
The offline deterministic backend in this repository already scores 88%. A frontier
model would improve fluency and handle multi-hop questions; it would not fix table
extraction or the out-of-scope problem.

---

## 2. Data-quality triage agent

### The problem

The validation suite raises alerts. Each one needs an analyst to gather the same evidence
in the same order — price history, corporate actions around the date, index weight, how
comparable securities moved, what a second vendor says — before forming a view. The
gathering is mechanical and takes fifteen to thirty minutes. The judgement takes two.

### What was built

`src/miniftse/agents/triage.py`. An alert arrives; the agent runs a fixed toolkit of
**read-only** queries, ranks hypotheses against a rule-based decision tree, and drafts a
note for the duty analyst.

Two decisions matter:

**The tools investigate; the model only writes.** Every piece of evidence in a triage
note comes from a deterministic query against real data. The model's reasoning may be
wrong, but the evidence never is, and a reviewer can check it in seconds.

**Hypothesis ranking is rule-based, not model-generated.** Root-cause analysis on data
incidents is a well-trodden decision tree. Encoding it gives consistent triage across
analysts and shifts, an auditable rationale, and something that still works when the
model is unavailable. The single most discriminating test is the peer check: if a
security is down 30% and its peers are down 28%, it is a market event; if the peers are
flat, it is a data problem.

**There is no `fix_price` tool, and there should not be.** The output is a
recommendation. Applying it is a separate, human, logged action.

### Measured result

Against the 12-fault chaos drill (`make chaos-drill`): the validation layer detects
**11 of 11** injectable faults and blocks publication on all of them; the agent produces
a confident hypothesis on the large majority, with the peer check doing most of the work.

The drill's real output is the *gaps*, and building it found four genuine defects in my
own checks — including an inverted FX rate of 9.27 that sailed through a plausible-range
test, because 9.27 is a plausible number. Only comparison against yesterday's rate
catches it. That lesson generalises: absolute bounds catch corruption, and only history
catches a wrong-but-valid value.

### Cost and effort

Four to six weeks. The tools are thin wrappers over queries that already exist. Most of
the work is the decision tree, which means sitting with the operations team.

Time saved: 20 minutes on perhaps 600 alerts a year is 200 hours, and — more valuable —
it compresses the window between an alert firing and a human understanding it, which is
exactly the window in which a bad number gets published.

---

## 3. Client-response drafter

### The problem

"Why did the index return 8.2% when our fund returned 7.9%?" arrives several times a
week. The numbers take five minutes to compute and the prose takes forty to write
carefully — and it must be written carefully, because it is going to a client.

### What was built

`src/miniftse/agents/drafter.py`, structured around one guardrail:

1. Code computes the numbers into a `FactPack`, each with its provenance.
2. The model receives the facts and writes prose around them.
3. `NumberGuard` extracts **every numeral** from the draft and checks it against the
   pack. Anything unaccounted for blocks the draft.

Step 3 is what makes this usable in front of clients, and it is mechanical — a regex and
a set difference. The safety property does not depend on the model behaving.

Rounding is permitted, invention is not: a fact of 2.34% may be written as 2.3% or 2%,
but 0.42% appearing from nowhere blocks the draft with the offending figure named.

Nothing is sent automatically. `approved_for_send` is always `False` on creation.

### Cost and effort

Three to four weeks. The fact-pack builders are the work; the drafting is comparatively
trivial.

Time saved: ~150 hours a year, and more consistent responses.

**This is the one I would insist on measuring hardest before wider deployment.** The
failure mode is a wrong number in front of a client, and although the guard makes that
structurally difficult, "structurally difficult" is not "impossible" — a *correct* number
used in a *wrong context* passes the guard cleanly.

---

## Where these tools must not go

Stated explicitly, because the boundary is the proposal's credibility:

**Never in the index calculation path.** Not as a fallback, not to fill a gap, not to
estimate a missing price. A published index must be reproducible from its inputs and its
rules; a sampled model is neither. There is no `--use-llm` flag anywhere in the
calculation code and there should never be one.

**Never as an unchecked source of a number.** Enforced by `NumberGuard`, not by prompting.

**Never as the final approver of anything client-facing.** All three tools produce
drafts.

**Never trained on client data** without a specific, documented legal basis.

---

## Implementation order and why

**Methodology assistant first.** Highest volume, most contained risk, and it is the tool
that builds organisational confidence — a wrong answer wastes ten minutes, and every
answer carries a citation the reader can check.

**Triage agent second.** Higher value per use, but it needs the operations team's
decision tree, which needs their time.

**Client drafter last.** Highest risk. Build it once the guardrail patterns are
established and trusted, and pilot it on internal enquiries before external ones.

---

## What I would want to be held to

- The eval suite is a CI gate, so the accuracy figure in this document cannot silently
  become false.
- Every tool ships with its failure modes documented, as above. A tool whose limitations
  are unknown is not ready.
- Six months after launch, report *measured* time saved, not projected. If it is not
  there, say so and stop.

The asymmetry worth naming: these tools are cheap to build and their value is easy to
overstate. The discipline that makes them worth having is the same discipline that makes
an index worth having — measure it, publish the method, and be explicit about what it
does not do.
