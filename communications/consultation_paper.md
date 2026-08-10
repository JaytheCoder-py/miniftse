# Consultation paper: proposed move to semi-annual size band reviews

**miniFTSE Global All Cap Index Series**
**Consultation opens 1 September 2026 · Responses by 31 October 2026**
**Reference: MFTSE-CP-2026-03**

---

## 1. Purpose

This paper seeks views on a proposal to change the frequency at which **size band
membership** is reviewed in the miniFTSE Global All Cap Index Series, from quarterly to
semi-annual, while leaving the quarterly cycle for **index eligibility** unchanged.

We invite responses from all users of the index series. The five specific questions are
in section 6.

Under our Statement of Principles, a material methodology change requires public
consultation before implementation. No decision has been taken.

---

## 2. Background

The index is currently reviewed four times a year. At each review we re-test eligibility,
re-rank the universe by free-float market capitalisation, reassign size bands subject to a
two-percentage-point buffer, and recompute capping factors.

Size band changes are the second largest contributor to index turnover, after additions
and deletions. Because a band change forces every fund tracking either the source or the
destination index to trade, its cost falls on investors — and unlike an eligibility
change, it conveys no new information about whether a company belongs in the index at all.

The buffer already suppresses the most wasteful of these moves. This proposal asks
whether reducing the *frequency* of band reassessment would reduce cost further without
materially degrading how well the bands describe the market.

---

## 3. Proposal

**From the June 2027 review, size band membership would be reassessed only at the June
and December reviews.** The March and September reviews would continue to apply
eligibility screens, additions and deletions, share and float updates, capping, and fast
entry, exactly as now.

A security joining the index at a March or September review would be assigned to a band
on entry using the prevailing boundaries; it simply would not be *reassessed* until the
next June or December.

The buffer would be unchanged at two percentage points.

---

## 4. Impact analysis

Analysis over ten years of index history. Method and code are published alongside this
paper; the figures are reproducible from run manifest `MFTSE-GLOBAL-2026-06-30`.

### 4.1 Turnover

| Measure | Current (quarterly) | Proposed (semi-annual) | Change |
|---|---|---|---|
| Mean one-way turnover per review | 1.4% | 1.5% | +0.1pp |
| Implied annual one-way turnover | 5.6% | 3.0% | **−2.6pp** |
| Turnover attributable to band changes | 1.9% p.a. | 0.8% p.a. | **−1.1pp** |
| Round-trip band moves per year | 11 | 4 | **−64%** |

Per-review turnover rises slightly, because two reviews' worth of drift accumulates into
one. Annual turnover falls substantially, because the accumulated drift is smaller than
the sum of the individual moves — the round-trip figure is the reason: most of what is
saved is a company moving out of a band and back within a year, which is pure cost.

### 4.2 Index characteristics

| Measure | Current | Proposed | Change |
|---|---|---|---|
| Mean constituents, Large Cap | 248 | 247 | −1 |
| Tracking error, proposed vs current | — | 0.11% p.a. | — |
| Mean band purity | 97.8% | 94.1% | **−3.7pp** |
| Maximum single-name weight | 4.2% | 4.2% | unchanged |

"Band purity" is the share of constituents whose band matches what an unbuffered
quarterly review would have given. This is the cost of the proposal and we state it
plainly: the bands would describe the market slightly less precisely.

### 4.3 Estimated cost saving

At an assumed 15 basis points of round-trip trading cost, 2.6 percentage points of
annual turnover reduction is worth roughly **4 basis points a year** to a fund tracking
the index. For a £5bn tracker that is approximately £2m annually.

We would welcome challenge on the cost assumption. It is the figure the conclusion is
most sensitive to.

---

## 5. Alternatives considered

**5.1 Widen the buffer instead, from 2% to 4%.** Achieves a comparable turnover reduction
with a similar loss of band purity, and keeps the review cycle uniform. Rejected as the
lead proposal because the effect is less predictable: a wider buffer reduces turnover
only when companies happen to sit near boundaries, whereas a frequency change is
deterministic. We remain open to this and it is question Q3.

**5.2 Annual band reviews.** Roughly double the turnover saving, with band purity falling
to about 88%. We judge that too great a degradation, but the option is on the table if
respondents disagree.

**5.3 Do nothing.** Defensible. The current methodology works and every change imposes an
implementation cost on users. This is the counterfactual against which the proposal
should be judged, and "no change" is a legitimate response.

**5.4 Move to a fixed constituent count per band.** Rejected on principle rather than
cost: a fixed count makes band membership depend on how many companies happen to be
listed, which is not an economically meaningful quantity and varies with IPO cycles.

---

## 6. Questions for market participants

**Q1.** Do you support moving size band reassessment from quarterly to semi-annual? Please
indicate the reasoning behind your position.

**Q2.** Is a reduction in band purity from 97.8% to 94.1% acceptable in exchange for a
2.6 percentage point reduction in annual turnover? If not, what trade-off would be?

**Q3.** Would you prefer the alternative of widening the buffer from 2% to 4% while
retaining quarterly reviews? Please explain which properties matter most to you.

**Q4.** Are June and December the right months for band reassessment? Respondents with
year-end reporting or rebalancing constraints may prefer a different pair, and we would
rather know now.

**Q5.** Is a lead time from the March 2027 announcement to the June 2027 implementation
sufficient for your operational purposes? If not, what would be?

---

## 7. How to respond

Responses to `index.consultation@miniftse.example` quoting **MFTSE-CP-2026-03** by
**31 October 2026**.

Responses will be treated as confidential unless you indicate otherwise. We will publish
a summary of the feedback and the outcome in a market notice, together with our reasoning
— including where we have decided against the weight of responses and why.

---

## 8. Indicative timetable

| Date | Stage |
|---|---|
| 1 September 2026 | Consultation opens |
| 31 October 2026 | Consultation closes |
| November 2026 | Index Advisory Committee considers responses |
| December 2026 | Outcome published by market notice |
| March 2027 | Final rules confirmed |
| June 2027 | Implementation, if approved |

If the outcome is not to proceed, we will say so and explain why. A consultation that
only ever confirms the administrator's initial view is not a consultation.
