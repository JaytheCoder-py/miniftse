# miniFTSE Global All Cap Index Series — Ground Rules

**Version 1.0 · Effective 1 January 2026 · Administrator: miniFTSE Index Services**

> This is a training artefact. The index described is calculated on **simulated market
> data** and is not a real, licensable or investable benchmark. The document is written
> in the register of a real Ground Rules publication because that register — precise,
> numbered, unambiguous, defensible to a client — is the thing being practised.

---

## 1 Index Objective

### 1.1 Purpose

The miniFTSE Global All Cap Index measures the performance of the investable equity
opportunity set across developed and emerging markets. It is designed to be:

- **replicable** — every constituent is investable at scale by a foreign institutional
  investor, and the index weights are achievable without exceeding reasonable
  participation in daily traded volume;
- **rules-based** — every inclusion, exclusion and weight follows from published rules
  applied to observable data, with discretion confined to the exceptions in Section 11;
- **auditable** — every published level is reproducible from the input data, the rules
  in this document, and the recorded run manifest.

### 1.2 What the index is not

It is not an optimised portfolio, it does not target a factor exposure, and it makes no
claim to be efficient. Variant indices in this series (Section 12) take those objectives;
the parent index deliberately does not.

---

## 2 Index Structure

### 2.1 Size bands

The eligible universe is ranked by free-float market capitalisation and divided into
bands by cumulative share of total value:

| Band | Cumulative share of investable market value |
|---|---|
| Large Cap | top 70% |
| Mid Cap | next 15%, to 85% |
| Small Cap | next 13%, to 98% |
| Micro Cap | the remaining 2% |

The All Cap index comprises the **Large, Mid and Small Cap** bands. Micro Cap securities
are eligible for the universe but are not constituents of the All Cap index.

### 2.1.1 Determination of the cumulative share

The cumulative share is determined as follows, and the procedure is part of the rule
rather than an implementation detail. Two parties applying these Ground Rules to the same
universe must arrive at the same bands, on any machine.

1. **Ranking.** Securities are ranked by free-float market capitalisation, largest first.
   Where two securities have **identical** free-float market capitalisation, the security
   with the lower **primary identifier** — the SEDOL where one exists, otherwise the first
   available of ISIN, CUSIP, RIC or ticker — compared as a character string in ascending
   order, ranks higher. This tie-break is arbitrary; it exists so that the ranking is a
   total order and does not depend on the order in which the universe was assembled.

2. **Summation.** The cumulative total is the exactly rounded value of the sum of the
   free-float market capitalisations of the security and every security ranked above it.
   "Exactly rounded" means the arithmetic must not depend on the order or grouping in which
   the additions are performed.

3. **Precision.** The resulting cumulative share is rounded to **12 decimal places** before
   it is compared with any band boundary. All comparisons in this section and in Section
   8.3 are made on the rounded value.

4. **Boundary tie-break.** A security whose rounded cumulative share is **exactly equal**
   to a band boundary falls in the band that boundary **closes** — that is, the larger
   band. A security at exactly 70.000000000000% is Large Cap; a security at exactly
   85.000000000000% is Mid Cap.

The precision in (3) is chosen to sit far above the representational noise of the
calculation and far below any threshold at which a difference would be material: 1e-12 of
cumulative index weight is 1e-8 of a basis point.

### 2.2 Rationale for cumulative-value bands

Boundaries are set on cumulative value rather than on a fixed constituent count. A fixed
count makes band membership depend on how many companies happen to be listed, which
varies with IPO cycles and is not an economically meaningful quantity. Cumulative value
means "Large Cap" denotes the same share of the market in every period.

---

## 3 Eligible Securities

### 3.1 Security types

Eligible: ordinary shares, and real estate investment trusts.

Not eligible: warrants, convertible securities, unit trusts, and investment trusts.
These are excluded because their returns are a function of an underlying instrument or
portfolio rather than of a single operating company, so including them would double-count
exposure already present in the index.

### 3.2 Preferred shares

**Preferred shares are not eligible.** This excludes economically significant lines in
some markets — Brazilian, Korean and German preferreds among them — and is therefore a
live design decision rather than an obvious one. The rule is applied consistently in the
interests of comparability across markets; the alternative treatment is kept under
review.

### 3.3 Depositary receipts

Depositary receipts (ADRs and GDRs) are **excluded where the underlying local line is
itself a constituent**, and **eligible where it is not**.

The conditional form is deliberate. Holding both a local line and a receipt over the
same shares double-counts the issuer. But where the local line is inaccessible to foreign
investors, the receipt is the only investable route to that company, and excluding it
would make the index unrepresentative of the opportunity set actually available.

### 3.4 Multiple share classes

Where an issuer has more than one eligible line, **each line is assessed separately** for
eligibility and included on its own merits. The concentration limits of Section 6 are
applied at **issuer level**, aggregating across all lines of the same issuer.

---

## 4 Nationality

### 4.1 Assignment

Each security is assigned a single nationality, which determines its market
classification and therefore its index family membership. Assignment considers, in
descending weight:

1. country of headquarters and principal operations;
2. country of incorporation;
3. country of primary listing;
4. geographic concentration of revenue, where a single country accounts for at least 50%.

### 4.2 Offshore incorporation

Where a company is incorporated in a jurisdiction commonly used for legal or tax
convenience rather than operations — including the Cayman Islands and Bermuda — the
country of incorporation is **substantially discounted** in the assessment. Incorporation
in such a jurisdiction carries little information about where the company actually
operates.

### 4.3 Escalation

Where the evidence is conflicting, or where no single candidate country reaches a
confidence threshold of 55% with a clear margin over the runner-up, the assignment is
**referred to the Index Advisory Committee** rather than resolved by rule. Nationality
determines developed-versus-emerging classification, which drives substantial passive
capital flows; a marginal case is a governance decision, not an arithmetic one.

---

## 5 Eligibility Screens

All screens are assessed at the **review cut-off date** defined in Section 8.1, using
only data available on that date.

### 5.1 Free float

The free-float factor is the proportion of shares in issue available to ordinary
investors, after excluding strategic holdings: government stakes, founder and family
holdings, corporate cross-holdings, and restricted employee stock.

**Minimum free float for inclusion:**

| Market classification | Minimum free float |
|---|---|
| Developed | 5% |
| Advanced Emerging, Secondary Emerging, Frontier | 15% |

The higher emerging-market threshold reflects the greater prevalence of state and
founder ownership in those markets, and the fact that a nominally adequate float in a
shallow market may not be investable at index scale.

### 5.2 Foreign ownership limits

Where a market or a company imposes a limit on aggregate foreign ownership, the
**investable factor** is the lesser of the free-float factor and the remaining foreign
ownership headroom. The investable factor, not the free-float factor, is used both for
this screen and for weighting.

### 5.3 Liquidity

A security must have a **median daily traded value**, over the 250 trading sessions
ending at the cut-off, of at least **0.05% of its free-float market capitalisation**.

The **median** is specified rather than the mean. A single exceptional day — a takeover
rumour, an index event — should not qualify a security that is untradeable for the rest
of the year, and a mean-based screen is straightforward to game.

### 5.4 Price history

A security must have a traded price on at least **200 of the 250 trading sessions**
ending at the cut-off. This screens out securities with extended suspensions and recent
listings. Large new listings enter through the fast entry provision of Section 8.4
instead.

### 5.5 Minimum size

Free-float market capitalisation must be at least **USD 100 million** at the cut-off.

### 5.6 Incumbent relief

An existing constituent is tested against **75% of the entry threshold** on the free
float, liquidity and size screens, and against 50% of the price-history requirement.

A security must therefore fall materially below the entry bar before it is removed. Without
this asymmetry, a security oscillating around a threshold would enter and leave at
alternate reviews, generating turnover that costs tracking funds real money and conveys
no information.

### 5.7 Suspended securities

A suspended security is **not eligible to join** the index. An existing constituent that
is suspended is **retained** and valued at its last traded price, and is removed only
when the suspension is determined to be permanent or the security is formally delisted.

Removing a constituent on suspension would crystallise a price at which no investor can
transact. Where a suspension exceeds 20 trading days, the position is referred to the
Index Advisory Committee for a valuation decision, which may include writing the security
down to zero.

---

## 6 Weighting

### 6.1 Base weighting

Constituents are weighted by **free float adjusted market capitalisation** (that is,
free-float-adjusted market value), converted to
the index base currency:

```
weight_i  ∝  Price_i × Shares_i × InvestableFactor_i × CappingFactor_i × FX_i
```

### 6.2 Concentration limits

The index applies the **UCITS 5/10/40** diversification standard:

1. no single constituent may exceed **10%** of the index;
2. constituents individually exceeding **5%** may not together exceed **40%**.

Limits are applied at **issuer level**, aggregating all lines of the same issuer.

### 6.3 Capping method

Capping is applied by iteration: constituents breaching a limit are set at the limit and
held fixed, and the residual weight is redistributed proportionally among the remaining
constituents. The procedure repeats until no constituent breaches. Redistributing across
all constituents, including those already capped, does not converge.

Where the second limb cannot be satisfied at a 10% single-name cap, the effective cap is
lowered until it is, subject to a floor of 5%.

### 6.4 Application between reviews

**Capping factors are fixed at the review and are not recalculated between reviews.**

Prices move after a review, so a constituent may drift above its cap during the period.
This is expected and is not a breach. Re-capping daily would generate continuous turnover
for tracking funds and would defeat the purpose of a scheduled review. Constituents are
returned within their limits at the following review.

---

## 7 Index Calculation

### 7.1 Formula

```
Index Level_t  =  Σ_i ( P_i,t × S_i,t × F_i,t × C_i,t × FX_i,t )  /  D_t
```

where P is price, S shares in issue, F the investable factor, C the capping factor, FX
the rate into base currency, and D the divisor.

### 7.2 The divisor

The divisor exists so that the index level remains **continuous** across changes in index
market value that are not market movements. It has no units and no economic meaning, and
index levels are not comparable across index families.

On any non-market change:

```
D_new  =  D_old × ( MV_after / MV_before )
```

### 7.3 Which events move the divisor

| Event | Divisor | Reason |
|---|---|---|
| Price movement | unchanged | This is what the index is measuring. |
| Ordinary cash dividend | **unchanged** | The ex-date price fall is a genuine market movement, which a price index should show. The total return index recovers it by reinvestment. |
| Share split or bonus issue | **unchanged** | Price and share count change inversely; market value is identical. |
| Rights issue | changes | New capital enters the index and shareholders paid for it. |
| Return of capital | changes | Capital leaves the index. |
| Share count change | changes | Market value changes with no price movement. |
| Free-float change | changes | Investable market value changes with no price movement. |
| Constituent addition or deletion | changes | Membership changed. |
| Spin-off, spun entity **included** | unchanged | Value is retained within the index. |
| Spin-off, spun entity **excluded** | changes | Value leaves the index. |

### 7.4 Base date and base level

The index series is based at **1,000.00 on 4 January 2016**.

### 7.5 Return variants

Three series are published:

- **Price return (PR)** — capital only.
- **Gross total return (GTR)** — dividends reinvested in full on the **ex-date**, not
  the pay date.
- **Net total return (NTR)** — dividends reinvested after deduction of withholding tax
  at the rate applicable in the **issuer's country of domicile**.

### 7.6 Whose tax position the net return represents

The net return series represents a **notional non-resident institutional investor**
unable to reclaim withholding tax under a double taxation treaty. It is not the after-tax
return of any particular investor, and most real investors will do better.

Rates are applied by issuer domicile. The United Kingdom and Hong Kong levy no
withholding tax on dividends, so for constituents domiciled there the net and gross
contributions are identical.

### 7.7 Currency

Prices are converted at the WM/Reuters 4pm London closing spot rate. A currency-hedged
variant is described in Section 12.3.

---

## 8 Periodic Review

### 8.1 Calendar

The index is reviewed **quarterly**, effective on the third Friday of March, June,
September and December.

| Stage | Timing |
|---|---|
| Data cut-off | 35 calendar days before the effective date |
| Announcement | 14 calendar days before the effective date |
| Effective | third Friday of the review month |

### 8.2 Why there is a lag

The gap between announcement and effective date exists so that funds tracking the index
can trade the changes in an orderly way. An index that changed without notice would cost
its own trackers money at every review. The effective date coincides with derivatives
expiry, when market liquidity is deepest.

### 8.3 Size band buffers

An existing constituent is moved between size bands only when its cumulative-value rank
crosses the band boundary by more than **2 percentage points**.

Buffers suppress turnover from securities oscillating around a boundary. The cost is
stated plainly: with buffers, the bands no longer correspond exactly to their nominal
cumulative-value definitions, and two securities of identical size may sit in different
bands depending on which band they came from. This path dependence is accepted because
the turnover saving is worth more to investors than the definitional purity.

Buffers apply only to moves between **adjacent** bands. A security falling two bands in a
single review has not oscillated, and is moved.

**Determination and tie-break.** The cumulative share used in this section is the rounded
value determined under Section 2.1.1, and the distance from the boundary is itself rounded
to **12 decimal places** before it is compared with the buffer width. A constituent moves
only when it crosses the boundary by **more than** the buffer width; a constituent at
**exactly** the buffer width has not crossed by more than it, and is **held** in its
existing band. A constituent at exactly 72.000000000000% whose previous band was Large Cap
remains Large Cap.

### 8.4 Fast entry

A newly listed security - typically a large IPO - may enter outside the review
cycle where it:

1. was listed within the previous 365 days;
2. would rank in the **top 15%** of the investable universe by free-float market
   capitalisation;
3. meets the free float requirement of Section 5.1.

Fast entry is effective 5 trading days after announcement. The size bar is deliberately
high: a very large new listing left out until the next scheduled review would make the
index unrepresentative for months, but the exception must remain rarer than the rule.

### 8.5 Changes between reviews

Implemented immediately rather than deferred to the next review:

- a change in free float of more than **5 percentage points** in absolute terms;
- a change in shares in issue of more than **10%**;
- any corporate action under Section 9;
- deletion following a merger, acquisition or delisting.

Smaller changes are accumulated and applied at the next review.

---

## 9 Corporate Actions

### 9.1 Cash dividends

The price is reduced by the dividend amount on the ex-date. **The divisor does not
change.** The gross total return series reinvests the full amount across the index on the
ex-date; the net series reinvests the amount after withholding tax.

### 9.2 Special dividends

A special dividend exceeding **5% of the cum price** is treated as a **return of capital**
and adjusts the divisor. Below that threshold it is treated as ordinary income and does
not.

The threshold is a judgement. A large capital distribution is not income, and treating it
as such would show a price-index loss that shareholders did not experience.

### 9.3 Splits, reverse splits and bonus issues

Price and share count are adjusted by the ratio. Market value is unchanged, and **the
divisor is unchanged**. This is the most common source of index arithmetic error:
adjusting the price without the share count, or the reverse, changes market value and
silently moves the index.

### 9.4 Rights issues

On the ex-rights date the price is adjusted to the **theoretical ex-rights price (TERP)**
and the share count increased by the rights ratio. The divisor is adjusted for the new
capital.

```
TERP  =  ( N_held × P_cum  +  N_new × P_subscription )  /  ( N_held + N_new )
```

TERP is the weighted average of the shares a holder already owns and those they are
entitled to buy. A holder who takes up their rights is unaffected by the price fall,
which is why the fall must not be treated as a loss. **Worked example:** a 1-for-4 issue
at a 30% discount to a cum price of 100 has a subscription price of 70, so
TERP = (4 × 100 + 1 × 70) / 5 = **94**. The price falls 6%, the holder loses nothing, and
the divisor rises to absorb the subscribed capital.

### 9.5 Spin-offs

The parent's price is reduced by the value of the distributed entity.

Where the spun entity **meets the eligibility screens**, it enters the index at the
distributed value with the parent's investable and capping factors, total index market
value is preserved, and the divisor is unchanged.

Where it **does not**, the distributed value leaves the index, the divisor is adjusted,
and the distribution is credited to the total return series.

### 9.6 Mergers and acquisitions

**Cash consideration.** The target is valued at the offer price on the effective date and
then removed. The final movement to the offer price is recognised in the index return
before deletion — deleting at the previous close would erase the takeover premium and
flatter the index.

**Stock consideration.** The target is removed and the acquirer's share count increased by
the exchange ratio. Where the acquirer is not a constituent, the target's value leaves the
index and the acquirer is assessed for fast entry.

### 9.7 Delisting and insolvency

The security is valued at its final traded price, or at zero where no price is available,
and removed. **The loss is recognised in the index return before removal.** Removing a
failed constituent at its last good price would understate the loss actually borne by
investors, and is the mechanism by which survivorship bias enters an index.

---

## 10 Index Data and Publication

### 10.1 Publication

End-of-day levels for all three return variants are published by 22:00 London time on
each day that the majority of constituent markets are open.

### 10.2 Validation

No index level is published until it has passed the validation suite. Any check
classified as blocking prevents publication; any check classified as escalating
additionally requires sign-off by the duty analyst. **There is no software mechanism to
override a blocking check.** An override is a recorded human decision.

### 10.3 Reproducibility

Every published level is accompanied by a run manifest recording the code version, a
content hash of every input, the full parameter set and the software environment. Any
published number can be reproduced on demand.

---

## 11 Governance

### 11.1 Index Advisory Committee

The Committee reviews the application of these rules, decides cases referred under
Sections 4.3 and 5.7, and approves exceptions. It meets quarterly and on request.

### 11.2 Exceptions

An exception to these rules may be approved only by the Committee, must be recorded with
its reasoning, and must be disclosed in the next market notice. Exceptions do not create
precedent.

### 11.3 Methodology changes

Material changes to this document are subject to **public consultation** before
implementation. A consultation paper sets out the rationale, the proposal, a quantified
impact analysis, the alternatives considered, and specific questions to market
participants. Responses are considered by the Committee and the outcome is published in a
market notice.

Consultation is a regulatory obligation for a benchmark administrator, not a courtesy. It
exists because users of a benchmark make long-term decisions on the assumption that its
rules are stable, and are entitled to notice and a voice before they change.

### 11.4 Recalculation policy

Where an error is identified in a published level:

| Materiality | Action |
|---|---|
| Below 1 basis point | Corrected prospectively; no restatement. |
| 1 to 5 basis points | Corrected on the following business day; noted in a market notice. |
| Above 5 basis points | Index recalculated and restated; clients notified directly; market notice issued within one business day. |
| Any error affecting a review outcome | Recalculated regardless of size. |

The final row exists because an error that changes index membership has consequences for
tracking funds that are not proportional to the size of the level error.

### 11.5 Cessation

Where the Administrator determines that an index can no longer be calculated in
accordance with these rules, a cessation notice is published with no less than 90 days'
notice, together with guidance on the succession arrangements available to users.

---

## 12 Index Variants

### 12.1 Large/Mid Cap

Identical rules, restricted to the Large and Mid Cap bands.

### 12.2 Factor variants

Factor variants apply a tilt to the base weights of Section 6.1, using published factor
definitions and subject to a turnover budget. The methodology for each is published
separately.

### 12.3 Currency-hedged variants

Hedged variants sell forward the index's foreign currency exposure using one-month
forwards, reset monthly at the exposures prevailing on the reset date.

Because the hedge notional is fixed at the reset while the underlying value moves, the
hedge is imperfect within the month. The residual is **hedge error**, it is a structural
feature of any hedged index rather than an implementation defect, and it is largest in
exactly the volatile periods when investors examine it most closely.

---

## Appendix A — Glossary

**Buffer** — a zone around a band boundary within which an existing constituent is not
moved, used to reduce turnover.

**Capping factor** — a multiplier applied to a constituent's weight to enforce a
concentration limit. An index design artefact, not a property of the company.

**Divisor** — the denominator that keeps the index level continuous across non-market
changes in index market value.

**Fast entry** — admission of a large new listing outside the review cycle.

**Free float** — the proportion of shares in issue available to ordinary investors.

**Investable factor** — the lesser of the free-float factor and any foreign ownership
headroom.

**TERP** — theoretical ex-rights price; the price at which a stock should open after a
rights issue.

**Net total return** — total return after withholding tax at the issuer's domicile rate,
representing a notional non-resident institutional investor unable to reclaim treaty
relief.
