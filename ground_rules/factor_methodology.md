# Factor definitions

Generated from `miniftse.factors.definitions`. Editing this file by hand will not change the code, and the code is what computes the index.

### Value

Securities that are cheap relative to fundamentals have historically earned a premium. The economic story is contested and the choice between the two readings matters for how the index is sold: either cheap companies are riskier in ways a single-factor model misses and the premium is compensation, or investors systematically over-extrapolate bad news and the premium is a behavioural correction. The first implies the premium persists; the second implies it decays once it is widely known.

**Sub-signals**

| Signal | Definition | Weight | Direction |
|---|---|---|---|
| book_to_price | Book equity / full market cap | 30% | higher is better |
| earnings_to_price | Trailing 12m net income / market cap | 25% | higher is better |
| cashflow_to_price | Operating cash flow less capex / market cap | 25% | higher is better |
| sales_to_price | Trailing 12m revenue / market cap | 20% | higher is better |

**Processing**

Raw values are winsorised at the 1%/99% percentiles. They are then standardised by z-score, using a capitalisation-weighted mean so that the parent index scores zero. Scores are neutralised to industry by cross-sectional regression, and the residual is retained. Securities with no value receive the neutral score of zero.

Sub-signals are combined using the *integrated* method and the composite is re-standardised.

### Quality

Profitable, conservatively financed companies with earnings backed by cash outperform on a risk-adjusted basis. Gross profitability carries most of the weight because it is the hardest line for management to influence and the most comparable across accounting regimes.

**Sub-signals**

| Signal | Definition | Weight | Direction |
|---|---|---|---|
| gross_profitability | Gross profit / total assets | 35% | higher is better |
| return_on_equity | Trailing 12m net income / book equity | 25% | higher is better |
| accruals | (Net income - operating cash flow) / total assets | 20% | lower is better |
| leverage | Total debt / total assets | 20% | lower is better |

**Processing**

Raw values are winsorised at the 1%/99% percentiles. They are then standardised by z-score, using a capitalisation-weighted mean so that the parent index scores zero. Scores are neutralised to industry by cross-sectional regression, and the residual is retained. Securities with no value receive the neutral score of zero.

Sub-signals are combined using the *integrated* method and the composite is re-standardised.

### Momentum

Recent relative winners continue to outperform over horizons of three to twelve months, most plausibly because information diffuses slowly and investors under-react. Momentum is the highest-turnover of the classic factors and the one most damaged by transaction costs, which is exactly why an index wrapper needs a turnover budget rather than the raw signal.

**Sub-signals**

| Signal | Definition | Weight | Direction |
|---|---|---|---|
| momentum_12_1 | 12-month return, skipping the most recent month | 70% | higher is better |
| momentum_6_1 | 6-month return, skipping the most recent month | 30% | higher is better |

**Processing**

Raw values are winsorised at the 1%/99% percentiles using a median-absolute-deviation rule. They are then standardised by z-score, using a capitalisation-weighted mean so that the parent index scores zero. Scores are neutralised to industry by cross-sectional regression, and the residual is retained. Securities with no value receive the neutral score of zero.

Sub-signals are combined using the *integrated* method and the composite is re-standardised.

### Low Volatility

Low-risk securities have delivered higher risk-adjusted returns than the CAPM predicts. The leading explanation is leverage-constrained investors bidding up high-beta names to reach return targets they cannot reach with borrowing. Note the consequence for index design: the factor is defined on risk, so it is mechanically correlated with sector composition and demands industry neutralisation or it becomes a utilities-and-staples bet.

**Sub-signals**

| Signal | Definition | Weight | Direction |
|---|---|---|---|
| realised_volatility | Annualised 12-month daily volatility | 50% | lower is better |
| downside_volatility | Annualised volatility of negative days | 25% | lower is better |
| beta | 12-month beta to the equal-weighted universe | 25% | lower is better |

**Processing**

Raw values are winsorised at the 1%/99% percentiles. They are then standardised by z-score, using a capitalisation-weighted mean so that the parent index scores zero. Scores are neutralised to industry by cross-sectional regression, and the residual is retained. Securities with no value receive the neutral score of zero.

Sub-signals are combined using the *integrated* method and the composite is re-standardised.

### Size

Smaller companies have earned a premium over larger ones, though the effect is weak once microcaps and illiquid names are excluded, and much of the original evidence does not survive a realistic liquidity screen. Included because it is a standard risk-model factor and a necessary control, not because it is a compelling standalone index.

**Sub-signals**

| Signal | Definition | Weight | Direction |
|---|---|---|---|
| log_market_cap | Natural log of free-float market cap | 100% | lower is better |

**Processing**

Raw values are winsorised at the 1%/99% percentiles. They are then standardised by z-score, using a capitalisation-weighted mean so that the parent index scores zero. Securities with no value receive the neutral score of zero.

Sub-signals are combined using the *integrated* method and the composite is re-standardised.

### Investment

Companies that grow assets aggressively subsequently underperform. Consistent with both an over-investment story and simple mean reversion in returns on capital.

**Sub-signals**

| Signal | Definition | Weight | Direction |
|---|---|---|---|
| asset_growth | Year-on-year growth in total assets | 60% | lower is better |
| sales_growth | Year-on-year growth in revenue | 40% | lower is better |

**Processing**

Raw values are winsorised at the 1%/99% percentiles. They are then standardised by z-score, using a capitalisation-weighted mean so that the parent index scores zero. Scores are neutralised to industry by cross-sectional regression, and the residual is retained. Securities with no value receive the neutral score of zero.

Sub-signals are combined using the *integrated* method and the composite is re-standardised.

### Yield

High dividend payers have delivered a modest premium and materially lower volatility. The index-design trap is a yield trap: the highest yielders are often companies whose price has collapsed on a dividend the market expects to be cut, so a yield index without a quality overlay systematically buys impending dividend cuts.

**Sub-signals**

| Signal | Definition | Weight | Direction |
|---|---|---|---|
| dividend_yield | Trailing 12m dividends paid / market cap | 70% | higher is better |
| gross_profitability | Gross profit / total assets, as a quality overlay against yield traps | 30% | higher is better |

**Processing**

Raw values are winsorised at the 1%/99% percentiles. They are then standardised by z-score, using a capitalisation-weighted mean so that the parent index scores zero. Scores are neutralised to industry by cross-sectional regression, and the residual is retained. Securities with no value receive the neutral score of zero.

Sub-signals are combined using the *integrated* method and the composite is re-standardised.