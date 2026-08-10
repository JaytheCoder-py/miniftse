# Why our index level moved 30bp when nothing traded

**Module M2** · *Written for: a client relationship manager who needs to answer this today*

---

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
to floating-point precision. Our audit trail contains 7,335 such changes with zero
continuity breaches. If a client query ever pointed at one, we would find it in minutes,
because the record is kept for exactly this purpose rather than reconstructed afterwards.

---

*Calculated on simulated market data. Not an investable benchmark.*
