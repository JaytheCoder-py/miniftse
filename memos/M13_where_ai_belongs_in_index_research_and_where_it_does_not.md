# Where AI belongs in index research, and where it does not

**Module M13** · *Written for: the head of index research*

---

There is pressure to use language models more widely. This memo proposes where they earn
their place and, more importantly, where they must not go.

## Where they clearly help

**Answering methodology questions.** We maintain hundreds of pages of rules that
Research, Sales and Client Services all query constantly. An assistant that answers with
a page citation turns a twenty-minute document search into a thirty-second question. We
measure it: our current assistant answers a fixed set of methodology questions with
graded accuracy, and it **abstains rather than guessing** when the documents do not
contain the answer. The abstention behaviour matters more than the accuracy figure — a
confident wrong answer about an eligibility rule is worse than no answer.

**Triaging data alerts.** Given a failing check, an agent can gather the evidence a human
would gather — the price history, the corporate actions, a comparison against another
vendor — and produce a first-pass diagnosis. It proposes; a human decides.

**Drafting client responses.** With one absolute constraint, described below.

## The constraint that makes this safe

**Every number in a client-facing output is computed by code. The model writes prose
around numbers it is given, and never produces one itself.**

This is not a stylistic preference. A language model producing a plausible-looking
tracking error is the single most dangerous failure mode available to us, because the
output is indistinguishable from a correct one until a client acts on it. Our drafting
tool enforces this structurally: it is handed a computed result and asked to explain it.

## Where they must never go

Not in the index calculation path. Not as an unchecked source of any number. Not
client-facing without a human approving the specific message.

## What I would ask for

Three tools, in this order: the methodology assistant, the alert triage agent, the
response drafter. Each pays for itself in time saved within a quarter. None of them
requires us to trust a model with anything we cannot check.

---

*Calculated on simulated market data. Not an investable benchmark.*
