# Runbook — what to do when the 6am index calculation fails

**Audience:** the duty analyst. Assume you have been paged, it is early, and clients
expect levels by 22:00 London.

**The one rule:** *publishing late is recoverable; publishing wrong is not.* If you are
weighing speed against correctness, correctness wins, and nobody will second-guess you
for it.

---

## 0. First five minutes

```bash
uv run miniftse runs                 # what ran, when, and with what status
uv run miniftse daily                # re-run the DAG and see which step fails
```

Establish three things before touching anything:

1. **Which step failed?** The DAG names it. "The index failed" is not a diagnosis.
2. **Is it a data problem or a code problem?** Compare the run manifest against
   yesterday's — if the git SHA is unchanged and an input hash moved, it is data.
3. **What is the deadline?** Publication is 22:00 London. A failure at 06:00 gives you
   the day; a failure at 20:00 does not.

```python
from pathlib import Path
from miniftse.production.manifest import ManifestStore

store = ManifestStore(Path("artefacts/manifests"))
runs = store.list_runs()
today, yesterday = store.load(runs.iloc[-1].run_id), store.load(runs.iloc[-2].run_id)
print(today.explain_diff(yesterday))
```

That diff answers "code, config, or data?" in about five seconds, and that is the whole
of root-cause triage for a reproducibility failure.

---

## 1. `load_market_data` failed

**Almost always late data.** The step retries three times with a 30-second gap; if it has
exhausted them, the file genuinely is not there.

| Check | Action |
|---|---|
| Has the vendor file landed? | Wait and re-run. Late is normal. |
| Vendor incident? | Check their status page. Note the incident number. |
| Only one region missing? | Assess whether a partial calculation is defensible for this index. Usually it is not. |
| Nothing by 16:00 London? | Escalate to the Head of Index Operations and prepare a delay notice. |

**Do not** substitute yesterday's file to make the job green. A stale file produces a
plausible index that is wrong, and `stale_prices` will block it anyway — but only if the
data reaches the validation layer, which it will not if you patched it in upstream.

---

## 2. `validate_inputs` or `validate_output` failed

**Do not re-run.** This step deliberately has zero retries. A blocking check means the
data is wrong, not late, and running the same calculation again produces the same wrong
answer more slowly.

Read the findings. Each names the rule, the affected securities and the value that
triggered it.

### Triage the top finding

```python
from miniftse.agents.triage import TriageAgent, TriageToolkit

note = TriageAgent(toolkit=TriageToolkit(prices=..., corp_actions=..., weights=...)) \
    .triage(finding, as_of, peers=[...])
print(note.format())
```

The peer check is the discriminating test. Comparable securities moved the same way →
market event. They did not → data problem.

### Common findings

| Finding | Usual cause | Action |
|---|---|---|
| `price_outliers` | A genuine move, or a decimal error | Peer check. If peers moved too, note it and release. If not, check the raw vendor record. |
| `stale_prices` | A regional feed stopped | Identify the region. Do not carry prices forward silently. |
| `corp_actions_applied` | The event file arrived after the job started | Re-run from the corporate-actions step. |
| `divisor_continuity` | **An event was misclassified, or the rebase used the wrong baseline.** | **Stop. Do not publish.** The level is wrong. Escalate immediately. |
| `fx_continuity` | An inverted or badly-onboarded rate | Check `rate × prior_rate ≈ 1`. If so, it is inverted. |
| `constituents_priced` | A delisting processed in reference but not in the index | Check the security's status. |
| `max_weight` (WARN) | Ordinary price drift since the last review | Expected. Not a breach. See Ground Rules §6.4. |
| `max_weight` (BLOCK) | Capping did not run or did not converge | Re-run the review step and check the capping notes. |

---

## 3. `publication_gate` blocked

The gate is doing its job. **There is no software override, and that is deliberate.**

To publish anyway, three things must happen:

1. The duty analyst determines the finding is a false positive or immaterial.
2. **Someone with authority signs for it.** Not a config flag — a person.
3. The decision, its reasoning and the finding are recorded in the incident log and
   disclosed in the next market notice.

If you cannot get a signature, you do not publish. Issue a delay notice.

---

## 4. `calculate_index` failed

A code problem, or data that is malformed rather than merely wrong.

```bash
uv run miniftse check-golden     # has the code changed the index?
```

- **Golden master fails and no deliberate change was made** → a regression. Roll back to
  the last known-good SHA and rebuild.
- **Golden master passes** → today's data is the problem, not the code.
- **`IndexCalculationError: index emptied`** → the eligibility screens rejected
  everything, which is essentially always a data problem: a float file that failed to
  load, a volume field in the wrong units, an FX table that did not arrive.

---

## 5. `publish` failed

Retries twice on connection errors. If it has exhausted them the calculation is fine and
distribution is not.

The index has been calculated and validated. Write the output to object storage manually
if necessary and notify consumers. **Do not recalculate** — you would be re-running a
process that already succeeded, and re-running is how a good number becomes a different
good number.

---

## 6. `notify` failed

Non-critical, by design. The index published. Tell downstream consumers by another route
and raise a ticket. This must never block a good index.

---

## Deciding whether to recalculate

Ground Rules §11.4:

| Materiality | Action |
|---|---|
| Below 1bp | Correct prospectively. No restatement. |
| 1–5bp | Correct next business day. Note in a market notice. |
| Above 5bp | Recalculate and restate. Notify clients directly. Market notice within one business day. |
| Any error affecting a review outcome | Recalculate regardless of size. |

The last row catches people out. An error that changed index *membership* has
consequences for tracking funds that are not proportional to the size of the level error
— they traded on it.

---

## Writing the client note

Write it **while** fixing, not after. It takes twenty minutes and you will not have
twenty minutes later.

Structure: what happened; what the impact is, in basis points and which index levels;
what has been corrected; what is being done to prevent recurrence. Plain language, no
speculation, no blame.

```python
from miniftse.agents.drafter import ClientResponseDrafter, FactPack
```

Every number in the note must come from a computation. The `NumberGuard` will block the
draft otherwise, which is the point.

---

## Escalation

| Situation | Escalate to |
|---|---|
| Any `divisor_continuity` failure | Head of Index Production, immediately |
| Publication delayed past 20:00 London | Head of Index Operations |
| Error above 5bp in a published level | Head of Index Production + Client Services |
| Any error affecting index membership | Index Advisory Committee |
| Suspected vendor data corruption | Data Operations + the vendor |

---

## Afterwards

Within two business days, write the post-incident report: summary, timeline, impact,
root cause, remediation, prevention. One page. Blameless — the question is what in the
*system* allowed this, not who typed it.

Then add the check that would have caught it, and add the fault to the chaos drill in
`src/miniftse/quality/faults.py`. An incident that does not produce a new test will
happen again.
