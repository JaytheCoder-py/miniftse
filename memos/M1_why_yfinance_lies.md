# Why yfinance lies

**Module 1, Practice P1.1** · Author: Jason Chung · Status: **DRAFT — not started**

> **Audience:** a competent non-specialist. An engineering manager, or an interviewer who
> wants to know whether you understand *why professional market data costs money*.
> No unexplained jargon. Every claim backed by a number you produced.

---

## How to work this

Data is already cached — `uv run python notebooks/m1_yfinance_probe.py`, then:

```python
from notebooks.m1_yfinance_probe import load
df = load("NVDA", "raw")    # views: "raw" (auto_adjust=False), "auto", "shares"
```

Fill in each section below. Delete the italic prompts as you answer them. Put charts in
`memos/figures/` and reference them.

---

## 1. The adjusted-close convention destroys information

*What does yfinance actually return for `Close` when you ask for unadjusted data? Check
NVDA around 2024-06-10 against what NVDA genuinely traded at that week. Then answer:*

- *Which of split-adjustment and dividend-adjustment does `auto_adjust=False` turn off?*
- *Can you recover the actual traded price on a past date from this API at all?*
- *You need market capitalisation on 2024-06-03 to build an index. Price × shares
  outstanding. Show what you get, and what the right answer is.*

**Finding:**

**Evidence:**

---

## 2. Delisted securities simply do not exist

*Four tickers in the probe returned nothing: TWTR, ATVI, VMW, K.*

- *What does the API do — error, empty frame, silent success? Does the failure mode differ
  by ticker? (Note the "possibly delisted; no timezone found" warning on VMW but not others.)*
- *Suppose you backtest a strategy over 2018–2026 on "the S&P 500 as it is today". Name the
  three distinct biases you have introduced, not just survivorship.*
- *Estimate the damage. Roughly what fraction of a large-cap universe disappears over 8
  years to M&A and delisting? What does that do to a measured return?*

**Finding:**

**Evidence:**

---

## 3. A spin-off breaks a naive total-return calculation

**This is the section that matters most. The acceptance bar is here.**

*Take GE. On 2023-01-04 it distributed GE HealthCare; on 2024-04-02, GE Vernova. Take MMM,
which distributed Solventum on 2024-04-01.*

- *Look at the parent's price series across the ex-date. How large is the apparent one-day
  move?*
- *Is that move recorded anywhere in the `Dividends` or `Stock Splits` columns?*
- *So: if you compute daily returns as `close.pct_change()`, what does your backtest believe
  happened to a holder of GE that day? What actually happened to their wealth?*
- *A shareholder did not lose money. Reconstruct the true total return of the ex-date,
  including the value of the shares received. State the entitlement ratio you used and
  where you sourced it.*

**Required deliverable — a single worked number:**

> On [date], a naive `pct_change` total return for [ticker] gives **X%**.
> The economically correct total return, including the spinco distribution, is **Y%**.
> The error is **Z basis points on a single day.**

*Z must exceed 100bp. State it in a box like the above so a reader cannot miss it.*

**Finding:**

**Evidence:**

---

## 4. Share counts do not survive corporate actions

*Pull the `shares` view for NVDA and AMZN across their splits.*

- *Is the historical share count restated onto the post-split basis, or left as-reported?*
- *Multiply your share series by your price series across the split date. Is market cap
  continuous? It must be — a split creates no value. If it is not, the two series are on
  inconsistent bases.*
- *Which names failed to return a share series at all?*

**Finding:**

**Evidence:**

---

## 5. Dual-class and depositary receipts: one issuer is not one security

*GOOGL vs GOOG. BABA (US ADR) vs 9988.HK (the Hong Kong primary line).*

- *Are GOOGL and GOOG the same company? The same security? Should an index hold one, the
  other, or both?*
- *If an index held both Alphabet lines, and you wanted to cap "Alphabet" at 5%, at what
  level of the hierarchy does the cap apply — and does that even have a unique answer?*
- *BABA is a receipt over foreign shares. Name three things that differ between the ADR and
  the primary line that would matter to an index: trading calendar, currency, and one more.*

**Finding:**

---

## 6. So what?

*Close it out in under 200 words. Not "yfinance is bad" — that is a cheap conclusion. The
real one is a claim about what a professional data product must supply that a free API
structurally cannot, and why an index provider spends 60% of its effort on data
correctness. Write the paragraph you would say out loud in an interview.*

---

## Open questions

*Anything you could not resolve. Being explicit about the boundary of what you verified is
worth more than papering over it — and it is exactly the register a Ground Rules document
uses.*
