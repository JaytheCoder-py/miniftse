# What we do when the 6am calculation fails

**Module M12** · *Written for: a new joiner on the operations rota*

---

The index has to publish before the market opens. When something breaks at 6am, the
worst outcome is not being late — it is publishing something wrong. This memo is about
the order to think in.

## The gate

Nothing publishes until every validation check passes. There is no override flag. If a
blocking check fails, the pipeline stops and a person has to make a decision, and that
decision is recorded. This is deliberate: an override that lives in a config file will
eventually be set at 6am by someone under pressure who intends to look at it properly
afterwards.

## Triage order

1. **Is the input data wrong, or is our calculation wrong?** The pipeline validates
   inputs before it computes anything, so a failure at that stage points at the vendor
   and a failure later points at us. This one distinction saves the most time.
2. **Is it transient?** Late data is common and the correct response is to wait — the
   job retries automatically. A failed validation check is *not* transient. The data will
   be just as wrong in ninety seconds, and retrying only delays the alert while burning
   the window before the deadline.
3. **What is the client impact?** Quantify it in index basis points before deciding
   anything. A check that fires on a name carrying 0.02% of the index is a different
   problem from one carrying 3%.

## The judgement call

Publishing late is visible, embarrassing, and recoverable. Publishing a wrong number is
often invisible for days, and by the time it surfaces, clients have traded on it and the
remedy is a formal recalculation with client notification and a regulatory dimension.

When in doubt, do not publish. Nobody has ever been criticised here for escalating a
number they were unsure about.

## What "wrong" tends to look like

Not a crash. A price off by a factor of ten, a dividend that arrived a day late, an
exchange rate the right size but the wrong way up. Each of these produces a plausible
index level. That is why the checks exist and why they are worth taking seriously when
they fire on a morning where everything otherwise looks normal.

---

*Calculated on simulated market data. Not an investable benchmark.*
