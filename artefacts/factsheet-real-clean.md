# miniFTSE Global All Cap

**Index code** `MFTSE-GLOBAL` · **Base currency** USD · **Base date** 2016-01-04 (= 1,000) · **Series begins** 2017-01-03 (rebased to 1,000)

*Data as at 2026-06-29. Generated 2026-08-13 from run `MFTSE-GLOBAL-2026-06-30-20260813T102313`, code `571f2e3899f9`.*

---

## Index levels

| Series | Level |
|---|---:|
| Price return | 3,470.96 |
| Gross total return | 4,280.95 |
| Net total return | 4,019.97 |
| Divisor | 13,744,074,590.7062 |
| Constituents | 144 |

## Performance

Gross total return, dividends reinvested on the ex-date.

| Period | Total | Annualised |
|---|---:|---:|
| 1 month | -3.49% | - |
| 3 months | +15.68% | - |
| 6 months | +6.37% | - |
| 1 year | +23.43% | - |
| 3 years | +98.98% | +25.78% |
| 5 years | +123.37% | +17.44% |
| Since inception | +328.10% | +16.62% |

## Calendar year returns

| Year | Price | Gross TR | Net TR |
|---|---:|---:|---:|
| 2018 | -10.19% | -7.70% | -8.46% |
| 2019 | +28.13% | +31.79% | +30.68% |
| 2020 | +19.86% | +22.82% | +21.92% |
| 2021 | +14.68% | +16.94% | +16.26% |
| 2022 | -10.03% | -8.02% | -8.63% |
| 2023 | +22.18% | +24.71% | +23.94% |
| 2024 | +35.04% | +37.51% | +36.76% |
| 2025 | +22.14% | +23.84% | +23.33% |
| 2026 | +7.00% | +7.71% | +7.50% |

## Risk

| Measure | Value |
|---|---:|
| Annualised return | +16.57% |
| Annualised volatility | +19.33% |
| Return / volatility | 0.86 |
| Sortino ratio | 1.10 |
| Maximum drawdown | -33.48% |
| Best day | +12.64% |
| Worst day | -11.50% |
| Positive days | +55.90% |

*Return / volatility is quoted against a zero risk-free rate. It is not a Sharpe ratio and should not be compared with one.*

## Turnover

| Reviews in period | 37 |
|---|---:|
| Mean one-way turnover per review | 3.00% |
| Implied annual one-way turnover | 12.00% |

## Methodology summary

- **Universe** securities meeting the eligibility screens in the Ground Rules, in the LARGE, MID, SMALL size bands.
- **Weighting** free-float market capitalisation, capped under 10%/5%/40% (UCITS diversification).
- **Review** 4 times a year, with 14 days between announcement and effective date.
- **Buffers** 2% around each size-band boundary, applied to incumbents only.
- **Total return** dividends reinvested on the ex-date. The net series applies withholding tax at the rate of the issuer's country of domicile, representing a notional non-resident institutional investor unable to reclaim treaty relief.

## Important information

This index is a research artefact. It is not a real, licensable or investable benchmark, and nothing here is investment advice. Past performance does not indicate future results.

The levels above were computed on **real market data** from free sources (SEC EDGAR (reference, shares, fundamentals) + Yahoo (prices, actions) + FRED (deposit rate)), held as snapshot `real-clean` (fingerprint `74a184534ab7`). Free data does not support an index of this construction, and the resulting figures are wrong in ways that are known, enumerated and unfixed at this price:

- **survivorship** — The universe comes from the SEC's current registrant list, so companies acquired or delisted during the window are absent entirely. Returns are measured on survivors only and are biased upward.
- **no_free_float** — No free source publishes free-float factors. Every security is set to 1.0, so float-capitalisation weighting degenerates to full-capitalisation weighting and the free-float eligibility screens cannot bind.
- **split_adjusted_prices** — Yahoo returns split-adjusted closes even with auto_adjust=False, so the as-traded price is unrecoverable and historical market capitalisation cannot be reconstructed from price x shares across a split.
- **no_spinoffs** — Yahoo's actions series carries dividends and splits only. A spin-off's ex-date price drop is therefore indistinguishable from a fall in value, and total return is understated on that day by the value of the distribution.
- **no_mergers** — Terminal events are absent, so a security that left the index by acquisition simply stops having prices rather than being removed at a known value.
- **current_ranking** — Candidates are taken in the SEC file's order, which is by current market capitalisation. Selecting a historical universe by today's ranking is look-ahead in the candidate pool. Index *membership* is still decided by the reconstitution rules at each review date, so the look-ahead does not reach the weights - but the pool it draws from is not what it would have been.
- **sic_not_point_in_time** — The SIC code is the issuer's current one, mapped to ICB level 1. Historical reclassification is invisible, so classification-driven turnover is absent.

Observed while fetching this particular snapshot:

- `no_price_history`: 1
- `dropped_no_prices`: 1
- `no_share_count`: 25
- `shares_backfilled_to_base`: 39

Full methodology: `ground_rules/miniftse_ground_rules.md`. Run manifest `MFTSE-GLOBAL-2026-06-30-20260813T102313` records the code version, input hashes and parameters needed to reproduce every number on this page.
