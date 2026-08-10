# Memo: custom index request — four-week deadline, eight-week data dependency

**To:** Head of Index Products
**From:** Index Research & Design
**Date:** 12 August 2026
**Subject:** Sanderson Pension Trust custom index — recommended path

---

## The situation

Sales has committed to Sanderson a custom low-carbon index in **four weeks**. The
methodology as scoped requires a supplier-emissions field that Data Operations estimates
at **eight weeks** to source, licence and validate. Research's view is that the available
proxy would introduce a bias material enough to affect which companies are excluded.

Three defensible positions, and they cannot all hold. This memo recommends a path.

## What each party is actually protecting

Worth separating from what each is asking for, because the asks conflict and the
underlying concerns mostly do not.

**Sales** is protecting a client relationship and a revenue commitment. The date was
given in good faith; the data dependency emerged afterwards.

**Data Operations** is protecting the integrity of the data estate. Eight weeks is not
padding — it is licensing, ingestion, reconciliation against a second source, and the
back-history needed to compute a trend. Compressing it means shipping a field nobody has
validated, and that field would then be used by every subsequent index that touches it.

**Research** is protecting the client from a bad product. The proxy estimates supplier
emissions from sector averages, so it is systematically wrong in a specific direction:
companies with unusually clean supply chains within a dirty sector are penalised, and the
reverse. That is not noise, it is bias, and it lands hardest on exactly the companies the
client most wants to distinguish.

Everyone is right. That is why it is a management decision and not a technical one.

## Recommendation

**Launch in four weeks with a methodology that does not use the supplier-emissions field
at all, and publish an enhancement to it at the December review.**

Not a proxy. A different, narrower index that is fully defensible on the data we have
today — direct emissions and energy intensity, which are licensed, validated and already
in production for two existing indices.

### Why this rather than the alternatives

**Ship with the proxy and correct later.** Rejected. Once an index is published, changing
its constituent-selection basis is a methodology change requiring consultation, notice
and a market notice. We would be committing ourselves to a public correction of a product
that is four months old, and the client would reasonably ask why we launched something we
already knew was biased.

**Tell Sales the date is impossible.** Rejected as a first move. It is technically
correct and commercially useless: it protects Research's position while leaving the
client problem entirely unsolved, and it is the reason Research sometimes gets routed
around.

**Compress the data timeline.** Rejected. Sourcing the field faster means skipping the
second-source reconciliation, which is the step that catches vendor errors. We would be
absorbing an unbounded, permanent risk into the data estate to meet a date on one
mandate.

### What the client actually gets

A defensible low-carbon index on the agreed date, whose limitations we state ourselves
rather than have discovered. Plus a published roadmap to the fuller methodology with a
date attached.

My experience is that clients respond considerably better to "here is what we can do
well now, here is what comes in December, and here is precisely why" than to a product
that quietly does less than they assumed.

## What each party needs to do

| Who | Action | By |
|---|---|---|
| Research | Draft the narrowed methodology and quantify what it does and does not capture versus the full version | 19 August |
| Sales | Take the narrowed scope back to Sanderson with the December roadmap. **This conversation happens this week.** | 16 August |
| Data Ops | Begin sourcing on the original eight-week timeline, unchanged | now |
| Product | Schedule the enhancement as a December review change, with consultation if it alters selection | 26 August |

## The conversation with Sanderson

Sales should own it, with Research available. The framing that works:

> "We can deliver a robust low-carbon index for your date using emissions data we have
> validated. Full supply-chain emissions require a dataset we are onboarding now, and
> rather than launch with an estimate we know is biased against certain companies, we
> will add it in December. Here is exactly what the first version captures and what it
> does not."

What we must not do is present the narrowed version as though it were the full scope. The
client will find out, and they will find out at the point they most need to trust us.

## The underlying problem

This will recur. Sales committed a date before Research and Data Operations had scoped
the data.

**Recommendation:** custom index proposals get a mandatory 48-hour data feasibility
review before any date is given to a client. It costs Sales two days and it eliminates
this entire class of conflict. I am happy to draft the process.

---

*I am comfortable with the recommendation being overruled — the commercial context may
carry weight I do not have. What I would ask is that if we launch with the proxy, the
decision and its rationale are recorded, so that the December correction is a documented
plan rather than something that looks like an error.*
