# Building a value index: exposure, turnover and what the three approaches actually cost

**miniFTSE Research** · Working paper

---

## Abstract

We construct a value index three ways from an identical universe under identical eligibility rules — selection, tilt and constrained optimisation — and compare them on factor exposure, tracking error, turnover and explainability. The three differ only in how the candidate set becomes weights, which isolates the weighting decision from every other design choice. We find the familiar trade-off triangle holds, and argue that explainability is systematically underweighted in the literature because it does not appear in any performance statistic.

**This paper is computed on simulated data.** Value predicts returns in that universe because it was constructed to. Nothing here is evidence about real markets; it is evidence about what the three construction methods do to a signal of a given strength.

---

## 1. Why the comparison is usually confounded

Published comparisons of factor index construction generally compare products from different providers, which differ in universe, eligibility screens, review calendar, capping and factor definition simultaneously. The weighting difference is then one of six confounded variables.

Here every variant shares one universe, one set of screens, one review calendar, one capping rule and one factor definition. The only difference is the weighting step. Whatever separates them is attributable.

## 2. Method

The parent is float-market-capitalisation weighted with UCITS 5/10/40 capping, reviewed quarterly with a 49-day gap from data cut-off to effective date. The value score combines book-to-price, earnings yield, cash-flow yield and sales-to-price, winsorised, standardised with a capitalisation-weighted mean so the parent scores zero, and neutralised to industry by cross-sectional regression.

Fundamentals are point-in-time throughout: every figure comes from a filing dated on or before the computation date, and trailing sums collapse restatements to the latest filing *known then*. Scores are computed at the review cut-off, never the effective date — using the effective date would grant the index seven weeks of foresight at every review.

## 3. Results

| Approach | Factor exposure | Active share | Tracking error | Annual turnover |
|---|---:|---:|---:|---:|
| Selection (top 30%) | 0.98 | 0.69 | 6.9% | 60% |
| Tilt (strength 1.0) | 1.46 | 0.42 | 6.4% | 45% |
| Optimised (TE ≤ 3%) | 1.29 | 0.59 | 6.2% | 80% |

Three observations.

**The tilt achieves the highest factor exposure.** This is initially surprising — selection concentrates into the cheapest third and might be expected to dominate. It does not, because concentration is not exposure: holding fewer names raises active share without necessarily raising the weighted-average score, and the cheapest names are disproportionately small, so capitalisation weighting within the selection gives them little weight.

**The optimiser's realised tracking error exceeds its constraint.** It is held to 3% ex-ante at every review and realises around 6%. This is a risk model failure, not an optimiser failure, and it is discussed in §5.

**Turnover ranks in the opposite order to explainability.** The cheapest approach to run is the hardest to describe in a sentence.

## 4. The degradation waterfall

The number that matters is not the paper result but what survives contact with implementation. In order: remove microcaps, apply the liquidity screen, add transaction costs at 15bp round-trip, impose a one-month implementation lag.

For the tilt, turnover of 45% a year at 15bp round-trip costs approximately 14 basis points annually. Against a factor premium plausibly in the range of 100–300bp, transaction costs alone consume a material fraction — and this is the *cheapest* of the three approaches. Selection, at 60% turnover, costs roughly double.

This is the calculation that should precede any decision about tilt strength, and it is why the turnover-budget sweep exists in the codebase rather than a tilt strength being asserted.

## 5. Why the risk model under-forecasts

The ex-ante tracking error is computed from exposures held fixed at the estimation date. The index rebalances quarterly, so realised active exposure decays between reviews and then jumps back. A fixed-exposure forecast cannot see that path and systematically misstates realised risk.

We report the bias statistic rather than tuning it away. A risk model that reports a comfortable number because it was fitted to the period it forecasts is worse than one that is visibly wrong, because nobody investigates the comfortable one.

## 6. Research integrity

Generating 200 signals with no predictive power and testing them on the same returns, the best reaches a t-statistic of roughly 3.2 — above the conventional significance bar, and meaningless. Any new factor proposed for publication should clear |t| > 3 with standard errors that account for cross-sectional and serial correlation, and should arrive with an economic story stated in advance.

## 7. Recommendation

For an investor seeking cheap value exposure within a turnover budget, the tilt. It delivers the highest factor exposure of the three, the lowest turnover, and it can be explained in one sentence to a board that will have to defend the allocation when value underperforms — which it will, for periods measured in years.

The optimised variant is technically the most efficient and we do not recommend it, for a reason that never appears in a performance table: when it behaves unexpectedly, the explanation is "the optimiser did it".

---

*Simulated data. Not investment advice, not a live benchmark, and not evidence about real markets.*
