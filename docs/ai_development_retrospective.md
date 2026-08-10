# AI-assisted development: a retrospective

**Module 13, Practice P13.4**

This platform is roughly ~20,000 lines across 68 modules with 89 tests, built with heavy AI assistance. This is an opinionated account of where that helped and where it actively hurt, with specific examples.

---

## Where it was decisively faster

**Breadth of scaffolding.** Producing a typed, documented module with a sensible API — the covariance estimators, the SQL cookbook, the provider Protocols — is where the speed-up is largest. These are well-specified problems with known shapes.

**Domain vocabulary as a starting point.** Getting from "I need a Barra-style risk model" to a working weighted cross-sectional regression with an industry collinearity constraint took minutes rather than a day of reading.

**Documentation that stays current.** Several documents here are generated from the code — the SQL cookbook, the vocabulary map, the factor methodology. That pattern is more attractive when writing the generator is cheap.

## Where it introduced bugs that tests caught

**Plausible-but-wrong domain logic.** The most dangerous category, because it reads correctly.

- *The capping factor never carried the tilt.* `C = capped/raw` is a reasonable-looking line. It silently reverted the entire product to its parent's weights. Caught by a plausibility check on realised tracking error, not by a test.
- *The divisor was rebased twice on removals.* Both the handler and the dispatcher applied the adjustment. Each was individually correct.
- *The hedge overlay added cumulative mark-to-market daily* rather than the daily change, more than doubling the hedged index over six years.

**Statistical methods applied outside their assumptions.** Brinson attribution was run across six years and twenty-four reviews, reporting +23.8% of active return against an actual +10.6%. The arithmetic was right; Brinson is a single-period method and the window was wrong.

**Simulation parameters that were internally consistent and economically wrong.** The synthetic universe compounded arithmetic returns, so volatility drag turned a 7% drift into a negative decade. Separately, beta was given a linear return premium, which made the low-volatility factor come out with the wrong sign — the real anomaly exists *because* the empirical security market line is flat.

## The pattern

Every one of those bugs was **locally plausible and globally wrong**. None would be caught by reading the diff; all were caught by running the system and asking whether the output made sense.

That has a direct consequence for how to work this way:

> **Property tests and hand-computed fixtures become more important, not less.** A hand-worked TERP of 94.00 on a 1-for-4 rights issue at a 30% discount cannot be argued with. An assertion that the code returns what the code returns can.

## What I would do differently

1. **Write the plausibility check before the implementation.** "A factor index should have tracking error in the 2–8% range" would have caught the capping bug on the first run instead of several sessions later.
2. **Never trust a diagnostic computed by the component it measures.** Every component reported success while the product was broken.
3. **Verify the simulation before trusting anything computed on it.** Several hours of factor research ran on a universe with a negative equity risk premium.
4. **Treat green tests as evidence about the tests.** The suite was green throughout the capping incident. It tested the components, and the defect was in the interface.

## For an index provider specifically

The reason this matters here more than elsewhere: a wrong number in a published index is a commercial and regulatory event, and it is usually invisible until a client acts on it. The failure mode of AI-assisted development — fast production of plausible, locally-correct, globally-wrong code — is precisely the failure mode this domain punishes hardest.

That is an argument for using it with strong verification, not for avoiding it. The verification was affordable *because* the implementation was fast.
