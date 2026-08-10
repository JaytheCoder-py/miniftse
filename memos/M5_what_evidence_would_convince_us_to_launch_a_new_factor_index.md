# What evidence would convince us to launch a new factor index

**Module M5** · *Written for: the index governance committee*

---

This memo proposes the evidentiary standard we should require before publishing rules
that other people will put money behind. It is deliberately demanding, and the reason is
commercial rather than academic: we are not deciding whether something is interesting, we
are deciding whether to publish a rule that clients will allocate to.

## The problem with the conventional bar

The usual test for a new factor is a t-statistic above 2, which corresponds to a one-in-
twenty chance of the result being noise. That bar is wrong here, and not by a little.

Hundreds of factors have been tested against the same few decades of market data. When
enough people search the same dataset, someone finds something impressive by chance. We
can demonstrate this directly: generating 200 signals with **no predictive power
whatsoever** and testing them on real returns, the best of them reaches a t-statistic of
around 3.2. That is above the conventional bar, and it means nothing.

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

---

*Calculated on simulated market data. Not an investable benchmark.*
