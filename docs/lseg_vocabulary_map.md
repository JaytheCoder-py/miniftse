# LSEG vocabulary map

Every concept this repository uses, mapped to the LSEG product that supplies it in production, the Worldscope item number where there is one, and the free source used here instead.

> Generated from `LsegDataProvider.FIELD_MAP` by `reporting/vocabulary.py`. The table and the adapter cannot drift apart, which matters — a vocabulary map that disagrees with the code is worse than none, because it is confidently wrong in an interview.

## The answer this table exists to support

> *"I built this against EDGAR XBRL and Ken French because I did not have Worldscope, but the equivalent items are WC01001 and WC03501, and the main difference I would expect is restatement handling — EDGAR gives me the filing date so I can be genuinely point-in-time, whereas a careless Worldscope pull returns the restated figure."*

---

## Fundamentals

| Concept | LSEG field | Worldscope | Free equivalent | Gotcha |
|---|---|---|---|---|
| Book Equity | `TR.F.TotShHoldEq` | `WC03501` | EDGAR StockholdersEquity | Worldscope standardises across accounting regimes; EDGAR is as-reported US GAAP. The difference shows up most in intangibles. |
| Capex | `TR.F.CAPEXTot` | `WC04601` | EDGAR PaymentsToAcquirePropertyPlantAndEquipment | Signed negative in some sources and positive in others. Getting this backwards inverts free cash flow. |
| Dividends Paid | `TR.F.DivPaidTot` | `WC04551` | EDGAR PaymentsOfDividends | Cash paid, not declared. The two differ across a year end. |
| Free Float | `TR.FreeFloatPct` | `WC08001` | inferred from iShares | See the Free float row above. This is the field you would license the product for. |
| Gross Profit | `TR.F.GrossProfIndPropTot` | `WC01100` | EDGAR GrossProfit | Often absent for financials, where the concept does not apply. A missing-data policy has to say what happens then. |
| Net Income | `TR.F.NetIncAfterTax` | `WC01751` | EDGAR NetIncomeLoss | Consolidated vs parent-only is a real choice in some markets and the default differs by vendor. |
| Operating Cashflow | `TR.F.NetCashFlowOp` | `WC04860` | EDGAR NetCashProvidedByUsedInOperatingActivities | Sign conventions differ between vendors. |
| Revenue | `TR.F.TotRevenue` | `WC01001` | EDGAR Revenues | The classic gotcha item. Note the reporting currency: Worldscope reports in the company's own, and comparing it to a base-currency market cap without converting makes a JPY reporter look a hundred times cheaper. |
| Shares Outstanding | `TR.F.ComShrOutsTot` | `WC05301` | EDGAR cover page / dei tags | Must be paired with free float to be usable for index weighting, and free float has no free source. |
| Total Assets | `TR.F.TotAssets` | `WC02999` | EDGAR Assets | Straightforward; rarely restated. |
| Total Debt | `TR.F.TotDebt` | `WC03255` | EDGAR (assembled from several tags) | No single US-GAAP tag for total debt - it has to be assembled, and the assembly rule is a judgement. |
| Free float | `TR.FreeFloatPct` | `WC08001` | inferred from iShares holdings | The single most valuable field with no free substitute. FTSE bands float to reduce churn, so the published factor is not the raw percentage. |

## Prices, reference data and estimates

| Concept | Product | LSEG field | Free equivalent | Gotcha |
|---|---|---|---|---|
| Price history | Datastream | `TR.PriceClose` | yfinance close | Datastream P is unadjusted and RI is the total return index. yfinance adjusts splits in place even with auto_adjust=False, so historical market cap cannot be reconstructed from it at all. |
| Total return index | Datastream | `TR.TotalReturn` | computed in calc/index.py | RI reinvests gross on the ex-date. A net series needs the withholding table for the issuer's domicile, not the investor's. |
| Consensus EPS | IBES | `TR.EPSMean` | not available free | Summary files are DATED. Using today's consensus for a past date is a look-ahead that makes any estimate-revision factor look far better than it is. |
| Estimate revisions | IBES | `TR.EPSMeanEstDate` | not available free | The detail file, not the summary tape, is what supports a revision factor - you need the analyst-level history to know when a view changed. |
| Actual EPS | IBES | `TR.EPSActValue` | EDGAR NetIncomeLoss / shares | The IBES actual is NOT the GAAP number. It is restated onto a basis comparable with the estimates, excluding items analysts excluded. Comparing an IBES estimate to a GAAP actual manufactures a surprise that did not occur. |
| Sector classification | FTSE Russell | `TR.ICBIndustryCode` | SIC from EDGAR; Yahoo sector | ICB is FTSE Russell's own scheme and differs from GICS. Reclassification causes real index turnover, so it must be point-in-time - and every free source returns today's classification for every historical date. |
| Entity identifier | PermID | `TR.OrganizationID` | PermID (free API) | The one LSEG-specific piece of infrastructure available free. Permanent across renames and redomiciles, which is exactly what a security master needs. |
| Fund flows | Lipper | `TR.FundNetFlow` | not available free | Relevant to knowing who tracks your index, which matters when estimating the market impact of a reconstitution. |
| Index constituents | FTSE Russell | `TR.IndexConstituentRIC` | iShares ETF holdings CSV | The ETF is a sampled tracking portfolio, not the index. Reconciling against it conflates fund decisions with index rules. |
| FX spot | Datastream | `TR.PriceClose on =R` | FRED DEX* series | Quote direction is the trap. FRED publishes DEXUSUK as USD per GBP but DEXJPUS as JPY per USD, so half the series need inverting. |
| Short rates | Datastream | `1MD=` | FRED IR3TIB / DGS3MO | Needed for covered-interest-parity forwards. Tenors are not comparable across the FRED series, and a CIP forward from mismatched tenors carries a basis that looks like hedge error. |
| Corporate actions | Datastream | `TR.CAAdjustmentFactor` | yfinance actions (dividends and splits only) | yfinance has no spin-offs, rights issues or mergers. A spin-off therefore appears as a large unexplained price drop, which is where the >100bp naive total-return error in the Module 1 memo comes from. |
| Delisted securities | Datastream | `dead RICs retained` | none free | yfinance returns an empty frame for a delisted ticker. Any universe built from it is survivorship-biased by construction, and the bias is invisible. |

---

## The four gaps a licence actually buys

Everything else in this repository was reproducible free. These were not:

1. **Free float.** No free substitute exists. It can be inferred from ETF holdings to within a few percent, which is enough to demonstrate the machinery and not enough to publish.
2. **Corporate action detail.** Spin-offs, rights issues and mergers are absent from every free price feed. This is the single largest source of silent error in a retail-data backtest.
3. **Analyst estimates.** IBES has no free equivalent at all, so an entire factor family — revisions, surprise, dispersion — cannot be built.
4. **Delisted securities.** Free feeds drop them, so any universe built from one is survivorship-biased by construction and the bias is invisible.

## Products, in one line each

- **Datastream** — global time series, deep history. Strength is length and breadth of coverage; mnemonics and adjustment conventions are the learning curve.
- **Worldscope** — global fundamentals standardised across accounting regimes. Item numbers (WC…), restatement handling, and the fiscal-versus-calendar-year problem.
- **IBES** — analyst estimates. Summary tape versus detail file; the IBES actual is not the GAAP number.
- **Lipper** — fund data, classifications and flows. Tells you who tracks your index.
- **LSEG Workspace** — the terminal, and the `lseg.data` Python library you would actually code against.
- **PermID** — open permanent identifiers, free API, genuinely useful.
