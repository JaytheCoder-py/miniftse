"""Client-facing factsheet generation.

Every number on this page comes from a computation, never from prose someone typed. It
matters for the same reason the AI layer refuses to let a model produce a figure: a
factsheet is a client document, the numbers get checked, and a hand-typed one goes stale
the first time the index is rebuilt.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from miniftse.production.build import BuildResult


def performance_table(levels: pd.DataFrame, column: str = "gross_total_return"
                      ) -> pd.DataFrame:
    """Standard trailing-period returns, annualised beyond one year."""
    frame = levels.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    series = frame.set_index("date")[column]
    end = series.index[-1]

    periods = {
        "1 month": 21, "3 months": 63, "6 months": 126, "1 year": 252,
        "3 years": 756, "5 years": 1260, "Since inception": len(series) - 1,
    }
    rows = []
    for label, days in periods.items():
        if days <= 0 or days >= len(series):
            if label != "Since inception":
                continue
            days = len(series) - 1
        start_value = float(series.iloc[-(days + 1)])
        end_value = float(series.iloc[-1])
        total = end_value / start_value - 1.0
        years = days / 252.0
        rows.append({
            "period": label,
            "total_return": total,
            "annualised": (1 + total) ** (1 / years) - 1 if years > 1 else np.nan,
        })
    del end
    return pd.DataFrame(rows)


def risk_table(levels: pd.DataFrame, column: str = "gross_total_return") -> dict[str, float]:
    frame = levels.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    series = frame.set_index("date")[column]
    returns = series.pct_change().dropna()
    years = (series.index[-1] - series.index[0]).days / 365.25

    ann_return = float((series.iloc[-1] / series.iloc[0]) ** (1 / years) - 1)
    ann_vol = float(returns.std() * np.sqrt(252))
    downside = returns[returns < 0]
    return {
        "annualised_return": ann_return,
        "annualised_volatility": ann_vol,
        "sharpe_ratio_zero_rf": ann_return / ann_vol if ann_vol else 0.0,
        "sortino_ratio": (
            ann_return / float(downside.std() * np.sqrt(252)) if len(downside) else 0.0
        ),
        "max_drawdown": float((series / series.cummax() - 1).min()),
        "best_day": float(returns.max()),
        "worst_day": float(returns.min()),
        "positive_days": float((returns > 0).mean()),
    }


def calendar_year_returns(levels: pd.DataFrame) -> pd.DataFrame:
    frame = levels.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.set_index("date")
    yearly = frame[["price_return", "gross_total_return", "net_total_return"]].resample(
        "YE").last().pct_change().dropna()
    yearly.index = yearly.index.year
    return yearly


def write_factsheet(result: BuildResult, out: Path) -> Path:
    """Render the factsheet to markdown."""
    history = result.history
    config = history.config
    levels = history.levels
    perf = performance_table(levels)
    risk = risk_table(levels)
    yearly = calendar_year_returns(levels)

    reviews = history.reviews
    mean_turnover = float(reviews["one_way_turnover"].mean()) if not reviews.empty else 0.0
    annual_turnover = mean_turnover * len(config.review.months)

    final = levels.iloc[-1]
    lines: list[str] = [
        f"# {config.name}",
        "",
        f"**Index code** `{config.index_id}` · **Base currency** "
        f"{config.base_currency} · **Base date** {config.base_date} "
        f"(= {config.base_level:,.0f})",
        "",
        f"*Data as at {final['date']!s}. Generated {dt.date.today()} from run "
        f"`{result.manifest.run_id}`, code `{result.manifest.git_sha[:12]}`.*",
        "",
        "---",
        "",
        "## Index levels",
        "",
        "| Series | Level |",
        "|---|---:|",
        f"| Price return | {final['price_return']:,.2f} |",
        f"| Gross total return | {final['gross_total_return']:,.2f} |",
        f"| Net total return | {final['net_total_return']:,.2f} |",
        f"| Divisor | {final['divisor']:,.4f} |",
        f"| Constituents | {int(final['n_constituents'])} |",
        "",
        "## Performance",
        "",
        "Gross total return, dividends reinvested on the ex-date.",
        "",
        "| Period | Total | Annualised |",
        "|---|---:|---:|",
    ]
    lines += [
        f"| {r.period} | {r.total_return:+.2%} | "
        + (f"{r.annualised:+.2%} |" if pd.notna(r.annualised) else "- |")
        for r in perf.itertuples(index=False)
    ]

    lines += ["", "## Calendar year returns", "",
              "| Year | Price | Gross TR | Net TR |", "|---|---:|---:|---:|"]
    lines += [
        f"| {year} | {row['price_return']:+.2%} | "
        f"{row['gross_total_return']:+.2%} | {row['net_total_return']:+.2%} |"
        for year, row in yearly.iterrows()
    ]

    lines += ["", "## Risk", "", "| Measure | Value |", "|---|---:|"]
    labels = {
        "annualised_return": "Annualised return",
        "annualised_volatility": "Annualised volatility",
        "sharpe_ratio_zero_rf": "Return / volatility",
        "sortino_ratio": "Sortino ratio",
        "max_drawdown": "Maximum drawdown",
        "best_day": "Best day", "worst_day": "Worst day",
        "positive_days": "Positive days",
    }
    for key, label in labels.items():
        value = risk[key]
        formatted = (f"{value:.2f}" if "ratio" in key or "/" in label
                     else f"{value:+.2%}")
        lines.append(f"| {label} | {formatted} |")
    lines.append("")
    lines.append(
        "*Return / volatility is quoted against a zero risk-free rate. It is not a "
        "Sharpe ratio and should not be compared with one.*"
    )

    lines += [
        "", "## Turnover", "",
        f"| Reviews in period | {len(reviews)} |",
        "|---|---:|",
        f"| Mean one-way turnover per review | {mean_turnover:.2%} |",
        f"| Implied annual one-way turnover | {annual_turnover:.2%} |",
        "",
        "## Methodology summary",
        "",
        f"- **Universe** securities meeting the eligibility screens in the Ground "
        f"Rules, in the {', '.join(config.size_bands)} size bands.",
        f"- **Weighting** free-float market capitalisation, capped under "
        f"{config.capping.max_single_weight:.0%}/"
        f"{config.capping.aggregate_threshold:.0%}/"
        f"{config.capping.aggregate_limit:.0%} (UCITS diversification).",
        f"- **Review** {len(config.review.months)} times a year, with "
        f"{config.review.announcement_lag_days} days between announcement and "
        "effective date.",
        f"- **Buffers** {config.banding.buffer_width:.0%} around each size-band "
        "boundary, applied to incumbents only.",
        "- **Total return** dividends reinvested on the ex-date. The net series "
        "applies withholding tax at the rate of the issuer's country of domicile, "
        "representing a notional non-resident institutional investor unable to reclaim "
        "treaty relief.",
        "",
        "## Important information",
        "",
        "This index is a research artefact built on **simulated market data**. The "
        "levels above are not the performance of any real security, portfolio or "
        "index, and nothing here is investment advice. Past performance - simulated or "
        "otherwise - does not indicate future results.",
        "",
        f"Full methodology: `ground_rules/miniftse_ground_rules.md`. Run manifest "
        f"`{result.manifest.run_id}` records the code version, input hashes and "
        "parameters needed to reproduce every number on this page.",
        "",
    ]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def factsheet_data(result: BuildResult) -> dict[str, Any]:
    """The same numbers as structured data, for the client-response drafter.

    The AI layer draws from this, never from the rendered markdown. Numbers come from
    computation; the model only arranges words around them.
    """
    levels = result.history.levels
    return {
        "index_id": result.history.config.index_id,
        "name": result.history.config.name,
        "as_of": str(levels.iloc[-1]["date"]),
        "levels": {
            "price_return": float(levels.iloc[-1]["price_return"]),
            "gross_total_return": float(levels.iloc[-1]["gross_total_return"]),
            "net_total_return": float(levels.iloc[-1]["net_total_return"]),
            "divisor": float(levels.iloc[-1]["divisor"]),
            "n_constituents": int(levels.iloc[-1]["n_constituents"]),
        },
        "performance": performance_table(levels).to_dict("records"),
        "risk": risk_table(levels),
        "calendar_years": calendar_year_returns(levels).to_dict("index"),
        "run_id": result.manifest.run_id,
        "git_sha": result.manifest.git_sha,
    }
