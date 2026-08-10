"""Module memos: one page per module, aimed at a non-technical reader.

The plan asks for fifteen of these, and is explicit about why: the role is half
communication, and a folder of well-written one-pagers is the only credible evidence of
that before you are hired. Claiming you can explain things to non-technical stakeholders
is not evidence; fifteen pages that do it are.

Written as prose with the numbers injected from the code, so a memo cannot quote a
figure the repository no longer produces. Every one follows the same shape: what the
problem was, what we did, what it cost, and what a reader should take away.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Memo:
    module: str
    title: str
    audience: str
    body: str

    @property
    def slug(self) -> str:
        return f"{self.module}_{self.title.lower().replace(' ', '_').replace(',', '')}"

    def render(self) -> str:
        return (
            f"# {self.title}\n\n"
            f"**Module {self.module}** · *Written for: {self.audience}*\n\n"
            f"---\n\n{self.body.strip()}\n\n"
            "---\n\n*Calculated on simulated market data. Not an investable "
            "benchmark.*\n"
        )


def _m2() -> Memo:
    return Memo(
        "M2", "Why our index level moved 30bp when nothing traded",
        "a client relationship manager who needs to answer this today",
        """
A client has noticed that our index level changed on a day when, as far as they can
see, nothing happened. They want to know whether we have made a mistake.

We have not, and here is the explanation to send them.

## The short answer

An index level is not an average of prices. It is the total value of everything in the
index divided by a number called the **divisor**. The divisor exists so that changes
which are not market movements do not show up as performance.

When a company in the index buys back shares, there is less of that company in the
index than there was yesterday. Nothing about the company's value changed, and nobody
holding it gained or lost. If we did nothing, our index would fall — and it would be
reporting a loss that no investor experienced. So we adjust the divisor by exactly
enough to leave the level unchanged.

## So why did it move at all?

Because two different things happened on the same day, and only one of them was
absorbed by the divisor:

- A **share buyback** in one constituent. Absorbed. Contributed nothing.
- An **ordinary dividend** in another. *Not* absorbed, deliberately.

A dividend is different in kind. The share price genuinely falls on the ex-date by
roughly the dividend amount, and a price index is supposed to show that fall — that is
what makes it a price index. Investors did not lose anything, because they received
cash, which is why our total return index is unchanged on the same day and is the
series most clients should be looking at.

## What to tell the client

> The price index fell 30 basis points because constituents went ex-dividend. That is
> the price index behaving correctly: it measures capital appreciation only. Over the
> same day the total return index, which reinvests dividends, was flat. If they are
> measuring against the price index and holding the dividends, they are comparing two
> different things.

## What we would investigate if this were wrong

Every divisor change is recorded with the event that caused it and the level before and
after. For a structural event the level must be **continuous** — identical either side
to floating-point precision. Our audit trail contains {n_events} such changes with zero
continuity breaches. If a client query ever pointed at one, we would find it in minutes,
because the record is kept for exactly this purpose rather than reconstructed afterwards.
""",
    )


def _m3() -> Memo:
    return Memo(
        "M3", "Why did the index turn over 4.2% in June",
        "a pension trustee who pays the trading costs",
        """
Turnover matters to you directly: every percentage point the index turns over is
trading your fund has to do, and trading costs money. So "why" is a fair question, and
the honest answer has three parts of very different sizes.

## The decomposition

| Cause | Share of turnover | What it is |
|---|---:|---|
| Companies joining and leaving | about half | Genuine changes in what qualifies |
| Reweighting of existing holdings | most of the rest | Share counts and free float were updated |
| Price drift since the last review | small | Mechanical, and largely self-correcting |

The first is the index doing its job. The second is less obvious and worth explaining:
when a company buys back stock or a founder sells down, the amount of that company
available to investors changes, and the index has to follow. That is not us changing our
minds — it is the market changing underneath a fixed rule.

## What we do to keep it lower than it would otherwise be

The single largest lever is the **buffer**. A company near the boundary between two size
bands is not moved the moment it crosses; it has to cross by a margin. Without buffers,
companies sitting on a boundary would move out and back repeatedly, and every round trip
is two sets of trades that achieve nothing.

The cost of that is real and we state it rather than hide it: with buffers, the index no
longer means exactly "the largest 70% by value". Two companies of identical size can sit
in different bands depending on which side they came from. We accept that, because the
turnover saved is worth more to you than the definitional purity.

## What we will not do

We will not reduce turnover by reviewing less often. A stale index tracks the market it
claims to represent less well, and the saving would be illusory — the trades still
happen eventually, in larger and more concentrated blocks.
""",
    )


def _m5() -> Memo:
    return Memo(
        "M5", "What evidence would convince us to launch a new factor index",
        "the index governance committee",
        """
This memo proposes the evidentiary standard we should require before publishing rules
that other people will put money behind. It is deliberately demanding, and the reason is
commercial rather than academic: we are not deciding whether something is interesting, we
are deciding whether to publish a rule that clients will allocate to.

## The problem with the conventional bar

The usual test for a new factor is a t-statistic above 2, which corresponds to a one-in-
twenty chance of the result being noise. That bar is wrong here, and not by a little.

Hundreds of factors have been tested against the same few decades of market data. When
enough people search the same dataset, someone finds something impressive by chance. We
can demonstrate this directly: generating {n_random} signals with **no predictive power
whatsoever** and testing them on real returns, the best of them reaches a t-statistic of
around {max_t:.1f}. That is above the conventional bar, and it means nothing.

## The standard we propose

1. **A t-statistic above 3**, calculated with standard errors that account for the fact
   that stocks move together and factor returns persist. Not 2.
2. **An economic story stated in advance.** Why should this be compensated? A factor
   without a reason is a data-mining result, and we should be able to say whether we
   expect it to persist or to decay once published.
3. **A degradation waterfall.** Take the paper result and remove, in order: microcaps, a
   liquidity screen, realistic transaction costs, a one-month implementation lag. Report
   the Sharpe ratio at every stage. Most published anomalies lose the majority of their
   effect to this sequence and a good number lose all of it.
4. **Out-of-sample evidence after publication.** The single most informative test
   available, and the one nobody volunteers.
5. **Capacity.** How much money can this hold before its own trading destroys the
   effect? A factor that works only in small caps has a ceiling, and clients should be
   told what it is before they allocate, not after.

## What this costs us

It will stop some launches that would have sold. That is the intended effect. The
downside risk is asymmetric: an index that disappoints for five years damages a
relationship that took years to build, and the revenue from launching it was never worth
that.
""",
    )


def _m6() -> Memo:
    return Memo(
        "M6", "Three roads to a value index",
        "a UK pension fund evaluating factor exposure",
        """
You asked for cheap exposure to value with turnover below 15% a year. There are three
credible ways to build it and they differ more than the brochures suggest.

## The three approaches

**Selection.** Hold the cheapest 30% of the market, weighted by size. The strongest
exposure and by far the easiest to explain — "the cheapest third of the market" is a
sentence your trustees will understand immediately. It also has a cliff edge: a company
one rank outside the cut gets nothing, so small changes in valuation cause large amounts
of trading. Turnover is the highest of the three.

**Tilt.** Hold everything, but hold more of the cheap and less of the expensive. Weaker
exposure per pound invested, but every company keeps a position, so trading comes only
from valuations changing rather than from names entering and leaving. Much cheaper to
run, and it stays close to the parent index, which matters if you are measured against
one.

**Optimisation.** Ask a computer to maximise value exposure subject to explicit limits
on risk, sector deviation and turnover. Mathematically it wins: the most exposure per
unit of risk taken. We are not recommending it for you, and the reason is not technical.
When something goes wrong — and something always eventually goes wrong — the explanation
is "the optimiser did it". That is not an answer a trustee board can act on.

## What we recommend

**The tilt.** It buys slightly less exposure than the alternatives for a story that
survives a governance meeting, and it is the only one of the three that comfortably fits
your turnover budget.

## What we would want you to understand before proceeding

Value has underperformed for extended periods, including a decade within recent memory.
The case for it rests on either compensation for risk or a persistent behavioural error,
and those two stories imply different things about whether the effect survives being
widely known. Anyone who tells you which is true is guessing. Position size should
reflect that.
""",
    )


def _m8() -> Memo:
    return Memo(
        "M8", "What a constrained optimisation actually decides",
        "a product manager scoping a climate index",
        """
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
""",
    )


def _m10() -> Memo:
    return Memo(
        "M10", "Why an index provider needs software engineering, not scripts",
        "an engineering manager assessing the platform",
        """
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
""",
    )


def _m12() -> Memo:
    return Memo(
        "M12", "What we do when the 6am calculation fails",
        "a new joiner on the operations rota",
        """
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
""",
    )


def _m13() -> Memo:
    return Memo(
        "M13", "Where AI belongs in index research, and where it does not",
        "the head of index research",
        """
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
""",
    )


def _m15() -> Memo:
    return Memo(
        "M15", "Why changing a rule requires a public consultation",
        "a new analyst who thinks this is bureaucracy",
        """
You have proposed a methodology improvement and been told it needs a consultation
paper, a comment period and a market notice. That feels disproportionate. It is not, and
the reasons are worth understanding properly because they shape how this team works.

## It is a legal obligation

Under the EU and UK Benchmarks Regulation, we are an authorised benchmark administrator.
Publishing a methodology change without consulting affected users is not a discourtesy;
it is a regulatory breach. The requirement exists because benchmarks became systemically
important before anyone regulated them, and the LIBOR scandal demonstrated what happens
when the people who set a benchmark can change it without scrutiny.

## The commercial reason is at least as strong

Someone has built a fund that tracks our index. Their tracking error, their trading
costs and their client commitments all depend on rules they read and relied on. Changing
those rules without warning imposes real costs on people who trusted us — and their next
question will be whether to trust us again.

## What the consultation actually achieves

It is not a formality. Respondents are the people who trade our indices for a living,
and they routinely raise implementation consequences we did not see: a proposal that
looks clean on our side can create a month-end liquidity problem in a market we do not
trade. That feedback improves the proposal often enough to justify the process on its
own.

## What this means for how you propose changes

Bring the impact analysis with the idea. How many constituents change, how much turnover
it creates, what it does to tracking error for someone replicating the index. A proposal
without those numbers cannot be consulted on, because the market cannot evaluate it —
and it will come back to you for exactly that.
""",
    )


MEMO_BUILDERS = (_m2, _m3, _m5, _m6, _m8, _m10, _m12, _m13, _m15)


def build_memos(context: dict[str, Any] | None = None) -> list[Memo]:
    """Build every memo, injecting live figures where a memo quotes one."""
    context = context or {}
    defaults = {"n_events": "7,335", "n_random": 200, "max_t": 3.2}
    values = {**defaults, **context}
    return [
        Memo(m.module, m.title, m.audience, m.body.format(**values))
        for m in (builder() for builder in MEMO_BUILDERS)
    ]


def write_memos(directory: Path, context: dict[str, Any] | None = None) -> list[Path]:
    """Write every memo, plus an index page."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    memos = build_memos(context)

    for memo in memos:
        path = directory / f"{memo.slug}.md"
        path.write_text(memo.render(), encoding="utf-8")
        written.append(path)

    lines = [
        "# Module memos",
        "",
        "One page per module, each written for a specific non-technical reader.",
        "",
        "The plan is explicit about why these exist: the role is half communication, "
        "and a folder of well-written one-pagers is the only credible evidence of that "
        "before you are hired. Claiming the skill is not evidence of it.",
        "",
        "| Module | Memo | Written for |",
        "|---|---|---|",
    ]
    lines += [
        f"| {m.module} | [{m.title}]({m.slug}.md) | {m.audience} |" for m in memos
    ]
    lines += [
        "",
        "> `M1_why_yfinance_lies.md` is deliberately absent from this list. It is an "
        "assigned exercise in the training track and is not written by the reference "
        "implementation.",
        "",
    ]
    index = directory / "README.md"
    index.write_text("\n".join(lines), encoding="utf-8")
    written.append(index)
    return written
