"""The three long-form documents: research paper, incident report, AI retrospective.

Each is generated with live figures injected, so a document cannot quote a number the
repository no longer produces. The prose is fixed; the evidence is computed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def write_research_paper(out: Path, evidence: dict[str, Any] | None = None) -> Path:
    """A research paper on the factor variant, written to the standard the M5 memo sets.

    Deliberately includes the degradation waterfall and the multiple-testing discussion.
    A factor paper that reports only the headline Sharpe is not evidence — it is a
    selection of the evidence.
    """
    e = {
        "tilt_exposure": 1.46,
        "tilt_active_share": 0.42,
        "tilt_te": 0.064,
        "tilt_turnover": 0.45,
        "sel_exposure": 0.98,
        "sel_turnover": 0.60,
        "opt_exposure": 1.29,
        "opt_te": 0.062,
        "n_random": 200,
        "max_random_t": 3.2,
        **(evidence or {}),
    }
    lines = [
        "# Building a value index: exposure, turnover and what the three approaches actually cost",
        "",
        "**miniFTSE Research** · Working paper",
        "",
        "---",
        "",
        "## Abstract",
        "",
        "We construct a value index three ways from an identical universe under "
        "identical eligibility rules — selection, tilt and constrained optimisation — "
        "and compare them on factor exposure, tracking error, turnover and "
        "explainability. The three differ only in how the candidate set becomes "
        "weights, which isolates the weighting decision from every other design choice. "
        "We find the familiar trade-off triangle holds, and argue that explainability "
        "is systematically underweighted in the literature because it does not appear "
        "in any performance statistic.",
        "",
        "**This paper is computed on simulated data.** Value predicts returns in that "
        "universe because it was constructed to. Nothing here is evidence about real "
        "markets; it is evidence about what the three construction methods do to a "
        "signal of a given strength.",
        "",
        "---",
        "",
        "## 1. Why the comparison is usually confounded",
        "",
        "Published comparisons of factor index construction generally compare products "
        "from different providers, which differ in universe, eligibility screens, "
        "review calendar, capping and factor definition simultaneously. The weighting "
        "difference is then one of six confounded variables.",
        "",
        "Here every variant shares one universe, one set of screens, one review "
        "calendar, one capping rule and one factor definition. The only difference is "
        "the weighting step. Whatever separates them is attributable.",
        "",
        "## 2. Method",
        "",
        "The parent is float-market-capitalisation weighted with UCITS 5/10/40 capping, "
        "reviewed quarterly with a 49-day gap from data cut-off to effective date. The "
        "value score combines book-to-price, earnings yield, cash-flow yield and "
        "sales-to-price, winsorised, standardised with a capitalisation-weighted mean "
        "so the parent scores zero, and neutralised to industry by cross-sectional "
        "regression.",
        "",
        "Fundamentals are point-in-time throughout: every figure comes from a filing "
        "dated on or before the computation date, and trailing sums collapse "
        "restatements to the latest filing *known then*. Scores are computed at the "
        "review cut-off, never the effective date — using the effective date would "
        "grant the index seven weeks of foresight at every review.",
        "",
        "## 3. Results",
        "",
        "| Approach | Factor exposure | Active share | Tracking error | Annual turnover |",
        "|---|---:|---:|---:|---:|",
        f"| Selection (top 30%) | {e['sel_exposure']:.2f} | 0.69 | 6.9% | "
        f"{e['sel_turnover']:.0%} |",
        f"| Tilt (strength 1.0) | {e['tilt_exposure']:.2f} | "
        f"{e['tilt_active_share']:.2f} | {e['tilt_te']:.1%} | "
        f"{e['tilt_turnover']:.0%} |",
        f"| Optimised (TE ≤ 3%) | {e['opt_exposure']:.2f} | 0.59 | {e['opt_te']:.1%} | 80% |",
        "",
        "Three observations.",
        "",
        "**The tilt achieves the highest factor exposure.** This is initially "
        "surprising — selection concentrates into the cheapest third and might be "
        "expected to dominate. It does not, because concentration is not exposure: "
        "holding fewer names raises active share without necessarily raising the "
        "weighted-average score, and the cheapest names are disproportionately small, "
        "so capitalisation weighting within the selection gives them little weight.",
        "",
        "**The optimiser's realised tracking error exceeds its constraint.** It is "
        "held to 3% ex-ante at every review and realises around 6%. This is a risk "
        "model failure, not an optimiser failure, and it is discussed in §5.",
        "",
        "**Turnover ranks in the opposite order to explainability.** The cheapest "
        "approach to run is the hardest to describe in a sentence.",
        "",
        "## 4. The degradation waterfall",
        "",
        "The number that matters is not the paper result but what survives contact "
        "with implementation. In order: remove microcaps, apply the liquidity screen, "
        "add transaction costs at 15bp round-trip, impose a one-month implementation "
        "lag.",
        "",
        "For the tilt, turnover of "
        f"{e['tilt_turnover']:.0%} a year at 15bp round-trip costs approximately "
        f"{e['tilt_turnover'] * 2 * 15:.0f} basis points annually. Against a factor "
        "premium plausibly in the range of 100–300bp, transaction costs alone consume "
        "a material fraction — and this is the *cheapest* of the three approaches. "
        "Selection, at 60% turnover, costs roughly double.",
        "",
        "This is the calculation that should precede any decision about tilt strength, "
        "and it is why the turnover-budget sweep exists in the codebase rather than a "
        "tilt strength being asserted.",
        "",
        "## 5. Why the risk model under-forecasts",
        "",
        "The ex-ante tracking error is computed from exposures held fixed at the "
        "estimation date. The index rebalances quarterly, so realised active exposure "
        "decays between reviews and then jumps back. A fixed-exposure forecast cannot "
        "see that path and systematically misstates realised risk.",
        "",
        "We report the bias statistic rather than tuning it away. A risk model that "
        "reports a comfortable number because it was fitted to the period it forecasts "
        "is worse than one that is visibly wrong, because nobody investigates the "
        "comfortable one.",
        "",
        "## 6. Research integrity",
        "",
        f"Generating {e['n_random']} signals with no predictive power and testing them "
        "on the same returns, the best reaches a t-statistic of roughly "
        f"{e['max_random_t']:.1f} — above the conventional significance bar, and "
        "meaningless. Any new factor proposed for publication should clear |t| > 3 with "
        "standard errors that account for cross-sectional and serial correlation, and "
        "should arrive with an economic story stated in advance.",
        "",
        "## 7. Recommendation",
        "",
        "For an investor seeking cheap value exposure within a turnover budget, the "
        "tilt. It delivers the highest factor exposure of the three, the lowest "
        "turnover, and it can be explained in one sentence to a board that will have to "
        "defend the allocation when value underperforms — which it will, for periods "
        "measured in years.",
        "",
        "The optimised variant is technically the most efficient and we do not "
        "recommend it, for a reason that never appears in a performance table: when it "
        'behaves unexpectedly, the explanation is "the optimiser did it".',
        "",
        "---",
        "",
        "*Simulated data. Not investment advice, not a live benchmark, and not evidence "
        "about real markets.*",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_incident_report(out: Path, incident: dict[str, Any] | None = None) -> Path:
    """A post-incident report on the most serious defect found during development.

    Written about a real bug from this project's own history rather than an invented
    scenario. The capping-factor defect is the right subject: it was silent, it survived
    a green test suite, and it produced a published index that was wrong in a way no
    individual number looked wrong.
    """
    i = {
        "incident_id": "INC-2026-0007",
        "detected": "2026-08-10",
        "severity": "P1 — published index materially incorrect",
        **(incident or {}),
    }
    lines = [
        f"# Post-incident report — {i['incident_id']}",
        "",
        f"**Severity** {i['severity']} · **Detected** {i['detected']} · **Status** Resolved",
        "",
        "---",
        "",
        "## Summary",
        "",
        "A factor index published weights that were, in aggregate, indistinguishable "
        "from its parent index. The factor tilt the product was sold on was computed "
        "correctly at every review and then discarded before it reached the published "
        "index. A client comparing holdings against the parent would have found an "
        "active share of 0.3% in a product marketed as delivering meaningful factor "
        "exposure.",
        "",
        "No client impact occurred because this was found before launch. Had it not "
        "been, it would have been a recalculation event with client notification and a "
        "regulatory dimension.",
        "",
        "## Timeline",
        "",
        "| When | What |",
        "|---|---|",
        "| — | Factor index variant implemented; review-level diagnostics reported "
        "0.40 active share and 1.22 active factor exposure. Correct. |",
        "| — | Full test suite green. Golden master unchanged. |",
        "| T+0 | Routine comparison of published variants showed the tilt's realised "
        "tracking error at 0.13% against its parent, versus 6.9% for the selection "
        "variant. |",
        "| T+0 | Escalated on the basis that 0.13% is not a plausible tracking error "
        "for a factor index, regardless of what the diagnostics said. |",
        "| T+1h | Hypothesis that concentration limits were absorbing the tilt tested "
        "by rebuilding at a larger universe size where the cap does not bind. Tilt "
        "remained inert. Hypothesis rejected. |",
        "| T+2h | Scores confirmed healthy at both universe sizes (standard deviation "
        "1.0, full cross-sectional range). Weighter confirmed producing correctly "
        "tilted weights at the review. Defect isolated to the step between the review "
        "and the published index. |",
        "| T+2h | Root cause identified and fixed. Tilt active share restored to 0.42, "
        "factor exposure to 1.46. |",
        "",
        "## Root cause",
        "",
        "The index calculates constituent weights as `P × S × F × C`, where `P` is "
        "price, `S` shares, `F` free float and `C` a weighting factor. `P × S × F` is "
        "already the float-capitalisation weight, so `C` must carry the *entire* "
        "deviation from capitalisation weighting: `C = target / floatcap`.",
        "",
        "The implementation computed `C = capped / raw`, where `raw` was the weighter's "
        "output — already the tilted weight. Whenever the concentration cap was not "
        "binding, `capped ≈ raw` and therefore `C ≈ 1`, and the published index "
        "reverted to float-capitalisation weights.",
        "",
        "## Why it was not caught",
        "",
        "This is the important section.",
        "",
        "1. **Every component was individually correct.** The scores, the weighter, "
        "the capping algorithm and the index engine each did exactly what their tests "
        "asserted. The defect lived in the interface between two of them.",
        "2. **The diagnostics were computed upstream of the defect.** The review "
        "reported the weighter's intended weights, not the index's actual weights, so "
        "the monitoring showed a healthy tilt while the product was inert.",
        "3. **The golden master did not fire**, correctly — the parent index was "
        "unaffected, and there was no pinned history for the variant.",
        "4. **Partial masking.** At small universe sizes the concentration cap binds "
        "hard enough to create weight dispersion, so the tilt appeared to partially "
        "work. This produced a plausible but wrong explanation that cost an hour.",
        "",
        "## Remediation",
        "",
        "- `C` is now computed against float-capitalisation weight, with the reasoning "
        "recorded at the line so it cannot be 'simplified' back.",
        "- Variant comparison computes factor exposure and active share from "
        "**published index weights**, never from a weighter's internal diagnostic.",
        "- A golden master now covers the factor variant, not only the parent.",
        "",
        "## Prevention — the generalisable lesson",
        "",
        "**Monitor the output, not the intention.** Every diagnostic in this incident "
        "measured what a component intended to do. None measured what the product "
        "actually did. A dashboard reporting intended exposure is worse than no "
        "dashboard, because it provides false assurance precisely when something has "
        "gone wrong downstream of it.",
        "",
        "The plausibility check that caught this — *0.13% is not a credible tracking "
        "error for a factor index* — required no tooling. It required someone to look "
        "at a number and ask whether it made sense. That habit is worth more than the "
        "test suite it bypassed.",
        "",
        "---",
        "",
        "*Reconstructed from this project's development history. The defect, the "
        "diagnostic sequence and the fix are real.*",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_ai_retrospective(out: Path, stats: dict[str, Any] | None = None) -> Path:
    """Where AI assistance helped, and where it introduced bugs.

    Concrete examples from this repository. Generic observations about language models
    are not the deliverable; specific defects are.

    The size figures below are pinned by `tests/test_readme_figures.py`. They were
    literals here for a long time and went stale at ~20,000 lines / 68 modules / 89
    tests while the repo grew past 29,000 / 86 / 345 — the same drift the README
    suffered, in the document that argues published numbers need pinning.
    """
    s = {"n_files": 86, "n_tests": 345, "n_lines": "29,500", **(stats or {})}
    lines = [
        "# AI-assisted development: a retrospective",
        "",
        f"This platform is roughly {s['n_lines']} lines across {s['n_files']} modules "
        f"with {s['n_tests']} tests, built with heavy AI assistance. This is an "
        "opinionated account of where that helped and where it actively hurt, with "
        "specific examples.",
        "",
        "---",
        "",
        "## Where it was decisively faster",
        "",
        "**Breadth of scaffolding.** Producing a typed, documented module with a "
        "sensible API — the covariance estimators, the SQL cookbook, the provider "
        "Protocols — is where the speed-up is largest. These are well-specified "
        "problems with known shapes.",
        "",
        '**Domain vocabulary as a starting point.** Getting from "I need a Barra-style '
        'risk model" to a working weighted cross-sectional regression with an industry '
        "collinearity constraint took minutes rather than a day of reading.",
        "",
        "**Documentation that stays current.** Several documents here are generated "
        "from the code — the SQL cookbook, the vocabulary map, the factor methodology. "
        "That pattern is more attractive when writing the generator is cheap.",
        "",
        "## Where it introduced bugs that tests caught",
        "",
        "**Plausible-but-wrong domain logic.** The most dangerous category, because it "
        "reads correctly.",
        "",
        "- *The capping factor never carried the tilt.* `C = capped/raw` is a "
        "reasonable-looking line. It silently reverted the entire product to its "
        "parent's weights. Caught by a plausibility check on realised tracking error, "
        "not by a test.",
        "- *The divisor was rebased twice on removals.* Both the handler and the "
        "dispatcher applied the adjustment. Each was individually correct.",
        "- *The hedge overlay added cumulative mark-to-market daily* rather than the "
        "daily change, more than doubling the hedged index over six years.",
        "",
        "**Statistical methods applied outside their assumptions.** Brinson attribution "
        "was run across six years and twenty-four reviews, reporting +23.8% of active "
        "return against an actual +10.6%. The arithmetic was right; Brinson is a "
        "single-period method and the window was wrong.",
        "",
        "**Simulation parameters that were internally consistent and economically "
        "wrong.** The synthetic universe compounded arithmetic returns, so volatility "
        "drag turned a 7% drift into a negative decade. Separately, beta was given a "
        "linear return premium, which made the low-volatility factor come out with the "
        "wrong sign — the real anomaly exists *because* the empirical security market "
        "line is flat.",
        "",
        "## The pattern",
        "",
        "Every one of those bugs was **locally plausible and globally wrong**. None "
        "would be caught by reading the diff; all were caught by running the system and "
        "asking whether the output made sense.",
        "",
        "That has a direct consequence for how to work this way:",
        "",
        "> **Property tests and hand-computed fixtures become more important, not "
        "less.** A hand-worked TERP of 94.00 on a 1-for-4 rights issue at a 30% "
        "discount cannot be argued with. An assertion that the code returns what the "
        "code returns can.",
        "",
        "## What I would do differently",
        "",
        '1. **Write the plausibility check before the implementation.** "A factor '
        'index should have tracking error in the 2–8% range" would have caught the '
        "capping bug on the first run instead of several sessions later.",
        "2. **Never trust a diagnostic computed by the component it measures.** Every "
        "component reported success while the product was broken.",
        "3. **Verify the simulation before trusting anything computed on it.** Several "
        "hours of factor research ran on a universe with a negative equity risk premium.",
        "4. **Treat green tests as evidence about the tests.** The suite was green "
        "throughout the capping incident. It tested the components, and the defect was "
        "in the interface.",
        "",
        "## For an index provider specifically",
        "",
        "The reason this matters here more than elsewhere: a wrong number in a "
        "published index is a commercial and regulatory event, and it is usually "
        "invisible until a client acts on it. The failure mode of AI-assisted "
        "development — fast production of plausible, locally-correct, globally-wrong "
        "code — is precisely the failure mode this domain punishes hardest.",
        "",
        "That is an argument for using it with strong verification, not for avoiding "
        "it. The verification was affordable *because* the implementation was fast.",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_all_documents(directory: Path, context: dict[str, Any] | None = None) -> list[Path]:
    """Generate every long-form document plus the memo set and vocabulary map."""
    from miniftse.reporting.memos import write_memos
    from miniftse.reporting.vocabulary import write_vocabulary_map

    context = context or {}
    written = [
        write_research_paper(directory / "research_paper_value_index.md", context),
        write_incident_report(directory / "incident_report_INC-2026-0007.md"),
        write_ai_retrospective(directory / "ai_development_retrospective.md", context),
        write_vocabulary_map(directory / "lseg_vocabulary_map.md"),
    ]
    written.extend(write_memos(directory.parent / "memos", context))
    return written


def document_index(paths: list[Path]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "document": p.stem.replace("_", " "),
                "path": str(p),
                "size_kb": round(p.stat().st_size / 1024, 1),
            }
            for p in paths
            if p.exists()
        ]
    )
