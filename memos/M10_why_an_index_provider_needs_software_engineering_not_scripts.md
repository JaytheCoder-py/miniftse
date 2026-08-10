# Why an index provider needs software engineering, not scripts

**Module M10** · *Written for: an engineering manager assessing the platform*

---

The calculation behind an index is arithmetic a competent analyst could do in a
spreadsheet. The reason this is a software engineering problem is not the maths.

## Three properties that a spreadsheet cannot provide

**Reproducibility.** A published index level is a commercial commitment. If a client
disputes a number from three years ago, we must be able to produce it again exactly.
That requires the code version, the input data version and the parameter set to be
recorded together with every output — and it requires the pipeline to be deterministic
end to end. We stamp every run with a manifest and can regenerate any historical run
from it. There is a test that proves this, and a second test that deliberately changes a
parameter and asserts the check *fails*, because a verification that has never failed is
not evidence of anything.

**Regression safety.** The index history is pinned to a hash. Any change to any part of
the system that alters a single published number fails the build immediately. This is
what makes it safe to keep improving the code: without it, every change is a gamble on
whether something moved that should not have.

**Invariants that hold by construction.** Some things must be true of an index in every
possible state: weights sum to one, the divisor never moves on a pure price change, the
level is continuous across every corporate action. We assert these against thousands of
randomly generated scenarios rather than against a handful of examples someone thought
of. That is how we found several of the bugs listed in the project history — including
one where a factor tilt was being computed correctly and then silently discarded before
it reached the published index.

## The honest summary

Most of the engineering here does not make the index better. It makes the index
*defensible*, which for a regulated, published, commercially-relied-upon product is the
same thing as making it usable.

---

*Calculated on simulated market data. Not an investable benchmark.*
