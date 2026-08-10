# What a constrained optimisation actually decides

**Module M8** · *Written for: a product manager scoping a climate index*

---

You have asked for an index that reduces carbon intensity by half, keeps sector weights
within two percent of the parent, caps any holding at five percent, and stays under
fifteen percent annual turnover. All four are achievable. This memo is about what they
cost, because constraints are not free and the cost is not visible in the final product.

## Constraints trade against each other, not against nothing

The optimiser's job is to get as close to the parent index as possible while satisfying
everything you asked for. Every constraint pushes it further away, and "further away" is
measured as tracking error — the amount by which this index will differ from the parent
in a typical year.

We can price each constraint individually by relaxing it and seeing how much tracking
error falls. That is the number to look at when deciding which requirements are real and
which are preferences. In our experience the sector constraint is usually the expensive
one: carbon intensity is concentrated in a handful of sectors, so demanding a large
reduction *and* near-parent sector weights is close to a contradiction, and the optimiser
pays for it in stock selection within those sectors.

## The failure mode to design against

If constraints conflict outright, the optimiser cannot produce a portfolio at all. An
index that fails to publish on a Tuesday is not an index, so the design must guarantee a
feasible answer exists. Ours does this by construction: the parent index itself always
satisfies every constraint, so "hold the parent" is always available as the answer of
last resort. It is never the answer we want, but it means we always have one.

That guarantee was added after the optimiser silently failed at every rebalance and fell
back to a simpler method while reporting success — a reminder that the dangerous failure
is not the loud one.

## What we will report to clients

Each constraint, and what it cost in tracking error. If a client is paying 40 basis
points of expected tracking error for a sector limit they did not think hard about, they
should be told, and given the chance to change it.

---

*Calculated on simulated market data. Not an investable benchmark.*
