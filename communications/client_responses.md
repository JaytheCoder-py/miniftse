# Five client responses

**Module 15, Practice P15.2.** Each written to the constraint that applies in the job:
under 300 words, no unexplained jargon, and explicit about what is known versus what
needs checking.

The hardest discipline in all five is the last one. The instinct under client pressure is
to sound certain. The professional move is to be precise about the boundary of your
certainty — a client who is told "we are checking X and will confirm by Thursday" is
better served than one given a confident answer that turns out to be wrong.

---

## 1. "Why is Company X in your Small Cap index when its market cap clearly qualifies for Mid Cap?"

Thank you for raising this — it is a fair question and the answer is a deliberate feature
of the methodology rather than an oversight.

Our size bands are not hard lines. A company is only moved between bands once it has
crossed the boundary by more than two percentage points of cumulative market value, and
that buffer applies only to companies already in the index. Company X is currently just
above the Mid/Small boundary, but not far enough above it to trigger a move.

The reason we do this is cost. Without a buffer, a company sitting near a boundary moves
band whenever its price wobbles, and every fund tracking either index has to trade each
time. Those trading costs are borne by investors and buy them nothing. The buffer means a
company must show a sustained change in size before we act.

The trade-off is real and worth stating plainly: with buffers, our bands no longer
correspond exactly to their nominal definitions, and two companies of identical size can
sit in different bands depending on where they came from. We accept that because we judge
the turnover saving to be worth more to investors than the definitional tidiness. This is
set out in section 8.3 of the Ground Rules.

If Company X remains above the boundary at the next quarterly review, it will move to
Mid Cap then. I am happy to send you the cumulative-value figures we used at the last
review if that would help.

---

## 2. "Your index returned 8.2% but our fund tracking it returned 7.9%. Explain."

A gap of that size is normal and expected. An index is a calculation; a fund is a
portfolio that has to actually hold things and pay for the privilege. The difference
comes from five places.

**Fees.** The index has none. Your fund's ongoing charge comes straight off the return.

**Transaction costs.** When the index changes constituents, the fund must trade. The
index records the change at a closing price; the fund pays a spread and moves the market
slightly against itself.

**Tax.** Our published return assumes a specific notional investor's withholding tax
position. Your fund's actual position depends on its domicile and the treaties available
to it, and will differ — sometimes favourably.

**Cash drag.** Dividends arrive on the pay date but the index reinvests them on the
ex-date, typically several weeks earlier. That cash earns nothing in the meantime.

**Sampling.** If the fund holds a representative subset rather than every constituent —
common for broad indices with a long tail of small names — that subset will not track
perfectly.

Thirty basis points over a year is within the range I would expect for a broad global
index. I would be glad to work through your fund's specific figures with you: your
manager can supply the fee and cost breakdown, and I can supply the index-side
attribution to sit alongside it. That comparison usually accounts for the gap
line by line.

---

## 3. "We want a version of your index that excludes tobacco and caps China at 15%. What's the tracking error and turnover impact?"

Both constraints are straightforward to implement, and I can give you indicative figures
now with the caveat that a proper answer requires running it on your preferred start
date.

**Tobacco exclusion.** A small number of large, stable, high-yielding companies. Removing
them typically adds modest tracking error — the effect is concentrated rather than
diffuse, so the number is sensitive to how those companies happen to perform. It also
tilts the index slightly away from high dividend yield and towards growth, which is worth
knowing if you have a yield target elsewhere in the portfolio.

**China capped at 15%.** The larger of the two constraints by some margin. It requires
redistributing weight to every other market, which affects the whole index rather than a
handful of names, and it adds turnover because the cap has to be reapplied at each
review as relative market sizes move.

**What I would want to check before quoting firm numbers:** your intended base date, and
whether the China cap should apply to companies by index nationality or by the exchange
they trade on. Those are different sets, and for Chinese companies the difference is
material.

I can run the full analysis — tracking error, turnover, and the cost of each constraint
separately — and come back within a week. I would suggest we also look at a 20% China cap
alongside, because the cost of these constraints is rarely linear and 15% may be paying
a lot for the last five percentage points.

---

## 4. "Your backtest shows a Sharpe of 0.9 but the live index has done 0.4 since launch. Was the backtest wrong?"

The honest answer is: probably not wrong, but it was measuring something the live index
cannot achieve. Both halves of that matter.

**What was not wrong.** The backtest applied the published rules to historical data. The
rules have not changed and the calculation was correct.

**What it could not capture.** A backtest is built with data as it exists today,
including restatements and vendor corrections that were not available at the time.
Reviews were applied on the theoretical schedule, without the announcement lags and
committee decisions that a live index has. And the universe was constructed from
securities that we can see now, which is not the same as the universe we would have seen
then.

**What is simply too short a sample.** This is the part I would emphasise most. The
difference between a Sharpe ratio of 0.9 and 0.4 over a short live period is well within
the range of ordinary randomness. Judging a strategy on a few years of live data is
statistically very weak, and it would be weak even if the backtest were perfect.

I can produce a line-by-line reconciliation from the backtest return to the live return,
quantifying each of the differences above and showing honestly what is left unexplained.
I think that is more useful than either defending the backtest or dismissing it.

What I would not tell you is that the live period is unrepresentative and the backtest
will reassert itself. I have no basis for saying that, and neither does anyone else.

---

## 5. "A stock in your index was suspended three weeks ago. How is it being valued and what happens if it never reopens?"

It is currently held at its last traded price and remains in the index.

**Why we do not remove it immediately.** Deleting a suspended security would require
striking it at some price, and there is no price at which anyone can transact. A fund
tracking the index cannot sell it, so removing it from the index would create a
difference between the index and every portfolio tracking it — the opposite of what a
benchmark should do.

**What happens next.** Because the suspension has passed twenty trading days, the
position is referred to our Index Advisory Committee for a valuation decision. The
Committee considers what is known about the company's circumstances and may keep the last
traded price, apply a write-down, or value it at zero.

**If it never reopens.** The security is written down to zero and removed, and — this is
the important part — the loss flows through the index return before the removal. We do
not delete it at the last good price. Doing so would erase a loss that investors actually
bore, which would make the index look better than the assets it represents.

**What I would check for you.** I can confirm the current carried value, the date of the
last trade, and the position's weight in the index. If the Committee has already met on
this security, I can tell you the decision and its date. Let me know and I will come back
today.
