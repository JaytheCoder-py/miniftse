# miniFTSE — building an index platform

**15 minutes.** Speaker notes in italics. Structure: the problem, the design decisions
and their trade-offs, one thing that went wrong, and what I would build next.

---

## 1 · What an index actually is  *(2 min)*

An index is not a backtest.

| A backtest | An index |
|---|---|
| Private simulation | Published, rule-bound object |
| Rerun when convenient | Calculated every day, on time |
| Wrong = disappointing | Wrong = a recalculation event |
| Answerable to you | Answerable to clients and a regulator |

*The distinction drives every design decision that follows. A backtest that is wrong
costs you a bad idea. A published index that is wrong has been traded against.*

**The whole mechanism in one line:**

```
Level = Σ (Price × Shares × Float × Cap × FX) / Divisor
```

The divisor absorbs every change that is **not** a market movement, so the level stays
continuous. Every methodology question about corporate actions reduces to: *does this
touch the divisor?*

---

## 2 · What I built  *(2 min)*

```
data → secmaster → corpactions → calc → universe/review
                                    ↓
                    factors → risk → optim → attrib
                                    ↓
                          quality → production → agents
```

500 securities, 2016–2026, quarterly reviews:

- **+3.7%** annualised, **17.1%** volatility, **−36.4%** max drawdown
- **7,590** divisor events, **zero** continuity breaches
- 345 tests, 27 validation rules, a pinned golden master

```bash
make setup && make test && make build-index
```

*No API keys, no network, no licence. That is a design decision, not a limitation —
slide 4.*

---

## 3 · Three decisions worth defending  *(4 min)*

### Capping binds at the review, not daily

Prices move after a review, so constituents drift above their cap. Re-capping daily would
force tracking funds to trade continuously and defeat the point of a scheduled review.

*My first validation rule treated the cap as a hard daily limit. It failed on clean data
at every review-to-review interval — which is precisely how a check gets switched off.
It now warns on drift and blocks only beyond 1.5×.*

### Buffers, and the cost of them

An incumbent must cross a size boundary by 2 percentage points before it moves.

| | Hard cut-off | 2% buffer |
|---|---|---|
| Annual turnover | 5.6% | 3.0% |
| Round-trip moves | 11/yr | 4/yr |
| Band purity | 100% | 94% |

*The purity number is the honest cost, and I would put it in front of a client rather
than let them find it. Two companies of identical size can sit in different bands
depending on where they came from. We accept that because the turnover saving is worth
more to investors than the definitional tidiness — but that is a judgement, and it should
be argued rather than assumed.*

### No language model ever produces a number

Code computes the numbers; the model writes prose around them; a guard checks every
numeral in the draft against the computed facts.

*A prompt instruction is not a safety property. A regex and a set difference is.*

---

## 4 · The synthetic universe  *(2 min)*

Everything runs on a deterministic generated market.

**Why:** a golden master needs bit-identical inputs, and real data is revised. A clean
clone must build a full index with no licence. And pathologies can be placed on known
dates so the corporate action engine can be tested against hand-computed values.

**What it cost me:** building a market taught me more about markets than consuming one.
Two examples, both of which I got wrong first:

- I compounded arithmetic returns, so σ²/2 volatility drag turned a ten-year index
  *negative*.
- I gave beta a linear return premium — and the low-volatility factor promptly came out
  with the wrong sign. The empirical security market line is flat, and **that flatness is
  the low-volatility anomaly**. A universe that prices beta linearly cannot contain the
  effect it claims to.

*Neither is a coding error. Both are modelling errors that only surfaced because
something downstream measured them.*

**The caveat I state everywhere:** value and quality predict returns in this world
because they were built to. Nothing computed here is evidence about real markets.

---

## 5 · What went wrong  *(3 min)*

**The golden master caught a 5.3 basis point drift between two identical builds.**

Same code. Same config. Same inputs. Different answer.

The cause: I seeded a spin-off's random number generator from `hash(security_id)`. Python
randomises string hashing per process. Every run produced different spin-off prices.

**Three things about this are worth more than the bug itself:**

1. **My determinism test missed it.** It ran on a small universe over a short window —
   which happens to contain no spin-offs. The test passed and proved nothing.
2. **Only the golden master could catch it.** No unit test would; every component was
   individually correct.
3. **This is exactly the class of defect run manifests exist for.** Code hash unchanged,
   input hashes unchanged, output hash changed. `explain_diff` now names it explicitly:
   *"Output changed with identical code, config and inputs. That is non-determinism.
   Treat as a defect, not a curiosity."*

*If a published index level moved for that reason, it would be a recalculation event and
a client notification, and the investigation would start from "which of our numbers can
we still trust?" — which is a much worse conversation than this one.*

**Twelve other real defects came out of the test suite.** They are listed in the README,
including a UCITS capping search that ran in the wrong direction and reported success, and
an inverted FX rate of 9.27 that passed a plausible-range check — because 9.27 is a
plausible number. Only comparison against yesterday catches that one.

---

## 6 · What I would build next  *(2 min)*

**Immediately**

1. **Real data behind the same interfaces.** The Protocols are the point; the LSEG
   adapter is a documented stub that raises rather than faking. Pointing it at Datastream
   and Worldscope should change nothing above `data/`.
2. **Close the eval gaps.** Five of 41 questions fail, mostly on markdown tables. Real
   Ground Rules are far more table-heavy than mine.

**Next**

3. **A real orchestrator.** The DAG models retries, dependencies and the gate; Dagster
   would supply the scheduler and the backfills.
4. **Cross-vendor reconciliation as a standing job.** The one check that catches a
   systematically wrong primary feed. Everything else validates data against itself.

**The thing I would argue for**

5. **Make the chaos drill a release gate.** Not a one-off exercise — every incident should
   add a fault to it, and the suite should have to catch all of them before a release
   ships. An incident that does not produce a new test will happen again.

---

## Closing

The engineering that matters here is not the maths. The index arithmetic is arithmetic.

What is hard is the discipline around it: publishing the rule *and* its cost, refusing to
override the gate, making a number reproducible three years later, and being specific
about what your system does not do.

*Every number on these slides is reproducible from the repository. The eval accuracy
figure is a CI gate, so it cannot silently become false.*

---

### Questions I would expect, and would enjoy

- *"Why not embeddings for retrieval?"* — small corpus, exact technical terms, no index
  to rebuild, and a bad retrieval can be explained. Embeddings are the upgrade when the
  corpus grows or paraphrase starts to matter.
- *"Your low-vol factor is only positive because you made it so."* — correct, and I would
  say so before being asked. The finding is not that low-vol works; it is that the
  toolkit recovers the premium the world was configured with, which is what makes it
  trustworthy when pointed at data I did not generate.
- *"5.3 basis points is tiny."* — the size is not the point. It was non-deterministic,
  and an index that gives two answers has no answer.
