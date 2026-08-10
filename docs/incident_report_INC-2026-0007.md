# Post-incident report — INC-2026-0007

**Severity** P1 — published index materially incorrect · **Detected** 2026-08-10 · **Status** Resolved

---

## Summary

A factor index published weights that were, in aggregate, indistinguishable from its parent index. The factor tilt the product was sold on was computed correctly at every review and then discarded before it reached the published index. A client comparing holdings against the parent would have found an active share of 0.3% in a product marketed as delivering meaningful factor exposure.

No client impact occurred because this was found before launch. Had it not been, it would have been a recalculation event with client notification and a regulatory dimension.

## Timeline

| When | What |
|---|---|
| — | Factor index variant implemented; review-level diagnostics reported 0.40 active share and 1.22 active factor exposure. Correct. |
| — | Full test suite green. Golden master unchanged. |
| T+0 | Routine comparison of published variants showed the tilt's realised tracking error at 0.13% against its parent, versus 6.9% for the selection variant. |
| T+0 | Escalated on the basis that 0.13% is not a plausible tracking error for a factor index, regardless of what the diagnostics said. |
| T+1h | Hypothesis that concentration limits were absorbing the tilt tested by rebuilding at a larger universe size where the cap does not bind. Tilt remained inert. Hypothesis rejected. |
| T+2h | Scores confirmed healthy at both universe sizes (standard deviation 1.0, full cross-sectional range). Weighter confirmed producing correctly tilted weights at the review. Defect isolated to the step between the review and the published index. |
| T+2h | Root cause identified and fixed. Tilt active share restored to 0.42, factor exposure to 1.46. |

## Root cause

The index calculates constituent weights as `P × S × F × C`, where `P` is price, `S` shares, `F` free float and `C` a weighting factor. `P × S × F` is already the float-capitalisation weight, so `C` must carry the *entire* deviation from capitalisation weighting: `C = target / floatcap`.

The implementation computed `C = capped / raw`, where `raw` was the weighter's output — already the tilted weight. Whenever the concentration cap was not binding, `capped ≈ raw` and therefore `C ≈ 1`, and the published index reverted to float-capitalisation weights.

## Why it was not caught

This is the important section.

1. **Every component was individually correct.** The scores, the weighter, the capping algorithm and the index engine each did exactly what their tests asserted. The defect lived in the interface between two of them.
2. **The diagnostics were computed upstream of the defect.** The review reported the weighter's intended weights, not the index's actual weights, so the monitoring showed a healthy tilt while the product was inert.
3. **The golden master did not fire**, correctly — the parent index was unaffected, and there was no pinned history for the variant.
4. **Partial masking.** At small universe sizes the concentration cap binds hard enough to create weight dispersion, so the tilt appeared to partially work. This produced a plausible but wrong explanation that cost an hour.

## Remediation

- `C` is now computed against float-capitalisation weight, with the reasoning recorded at the line so it cannot be 'simplified' back.
- Variant comparison computes factor exposure and active share from **published index weights**, never from a weighter's internal diagnostic.
- A golden master now covers the factor variant, not only the parent.

## Prevention — the generalisable lesson

**Monitor the output, not the intention.** Every diagnostic in this incident measured what a component intended to do. None measured what the product actually did. A dashboard reporting intended exposure is worse than no dashboard, because it provides false assurance precisely when something has gone wrong downstream of it.

The plausibility check that caught this — *0.13% is not a credible tracking error for a factor index* — required no tooling. It required someone to look at a number and ask whether it made sense. That habit is worth more than the test suite it bypassed.

---

*Reconstructed from this project's development history. The defect, the diagnostic sequence and the fix are real.*
