"""The rule set: twenty-four checks across the validation taxonomy.

Two design notes that matter more than any individual rule.

**Outlier detection must be relative, not absolute.** A fixed 5-sigma price threshold
fires on hundreds of securities in a crisis and on none in a calm month, so the alert
is loudest exactly when it is least informative and everyone learns to ignore it. Every
outlier rule here compares a security to the *cross-section that day*, so it asks "did
this move unusually relative to everything else" - a question whose answer does not
change with the regime.

**Severity is weighted by index impact.** A 40% one-day move on a 0.01% constituent is
a warning; on a 4% constituent it is a block. Rules that ignore weight either block
constantly or never block at all.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from miniftse.quality.rules import Finding, Rule, ValidationContext
from miniftse.types import Severity

# --------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------


def check_prices_present(ctx: ValidationContext) -> Finding:
    if ctx.prices is None or ctx.prices.empty:
        return ctx.fail("prices_present", "schema", Severity.ESCALATE,
                        "no price data at all for the run date")
    return ctx.ok("prices_present", "schema", Severity.ESCALATE,
                  f"{len(ctx.prices)} price rows")


def check_required_columns(ctx: ValidationContext) -> Finding:
    required = {"security_id", "date", "close", "currency"}
    if ctx.prices is None:
        return ctx.fail("required_columns", "schema", Severity.BLOCK, "no price frame")
    missing = required - set(ctx.prices.columns)
    if missing:
        return ctx.fail("required_columns", "schema", Severity.BLOCK,
                        f"price frame is missing {sorted(missing)}")
    return ctx.ok("required_columns", "schema", Severity.BLOCK, "schema conforms")


def check_no_duplicate_prices(ctx: ValidationContext) -> Finding:
    """Duplicate (security, date) rows.

    Silent and destructive: a duplicate does not error, it doubles a constituent's
    weight in any groupby-sum, and the index moves for no visible reason.
    """
    if ctx.prices is None:
        return ctx.ok("no_duplicate_prices", "schema", Severity.BLOCK, "no data")
    dupes = ctx.prices.duplicated(subset=["security_id", "date"], keep=False)
    if dupes.any():
        ids = ctx.prices.loc[dupes, "security_id"].unique()
        return ctx.fail("no_duplicate_prices", "schema", Severity.BLOCK,
                        f"{int(dupes.sum())} duplicate (security, date) rows",
                        n_affected=int(dupes.sum()),
                        sample=tuple(str(x) for x in ids[:5]))
    return ctx.ok("no_duplicate_prices", "schema", Severity.BLOCK, "no duplicates")


def check_no_null_prices(ctx: ValidationContext) -> Finding:
    if ctx.prices is None:
        return ctx.ok("no_null_prices", "schema", Severity.BLOCK, "no data")
    nulls = ctx.prices["close"].isna()
    if nulls.any():
        return ctx.fail("no_null_prices", "schema", Severity.BLOCK,
                        f"{int(nulls.sum())} null closing prices",
                        n_affected=int(nulls.sum()),
                        sample=tuple(ctx.prices.loc[nulls, "security_id"]
                                     .astype(str).head(5)))
    return ctx.ok("no_null_prices", "schema", Severity.BLOCK, "no nulls")


# --------------------------------------------------------------------------------------
# Range
# --------------------------------------------------------------------------------------


def check_positive_prices(ctx: ValidationContext) -> Finding:
    if ctx.prices is None:
        return ctx.ok("positive_prices", "range", Severity.BLOCK, "no data")
    bad = ctx.prices["close"] <= 0
    if bad.any():
        return ctx.fail("positive_prices", "range", Severity.BLOCK,
                        f"{int(bad.sum())} non-positive prices",
                        n_affected=int(bad.sum()),
                        sample=tuple(ctx.prices.loc[bad, "security_id"]
                                     .astype(str).head(5)))
    return ctx.ok("positive_prices", "range", Severity.BLOCK, "all prices positive")


def check_weights_sum_to_one(ctx: ValidationContext) -> Finding:
    if ctx.weights is None or ctx.weights.empty:
        return ctx.ok("weights_sum", "range", Severity.BLOCK, "no weights supplied")
    total = float(ctx.weights.sum())
    if abs(total - 1.0) > 1e-6:
        return ctx.fail("weights_sum", "range", Severity.BLOCK,
                        f"weights sum to {total:.10f}, not 1.0",
                        value=total, threshold=1.0)
    return ctx.ok("weights_sum", "range", Severity.BLOCK,
                  f"weights sum to {total:.12f}", value=total)


def check_weights_non_negative(ctx: ValidationContext) -> Finding:
    if ctx.weights is None:
        return ctx.ok("weights_non_negative", "range", Severity.BLOCK, "no weights")
    bad = ctx.weights[ctx.weights < -1e-12]
    if len(bad):
        return ctx.fail("weights_non_negative", "range", Severity.BLOCK,
                        f"{len(bad)} negative weights",
                        n_affected=len(bad),
                        sample=tuple(str(x) for x in bad.index[:5]))
    return ctx.ok("weights_non_negative", "range", Severity.BLOCK, "all non-negative")


def check_max_weight(ctx: ValidationContext) -> Finding:
    """Constituent weight against the published cap, allowing for post-review drift.

    The cap binds **at the review**, not every day. Capping factors are fixed when the
    review takes effect and prices then move, so a constituent that rallies hard will
    sit above the cap until the next review - and that is correct behaviour, not a
    breach. Forcing it back daily would generate continuous turnover for tracking funds
    and defeat the point of a scheduled review.

    So there are two thresholds. Modest drift above the cap is expected. A weight far
    beyond it means capping did not run, did not converge, or ran against the wrong
    universe - which is a genuine defect and blocks.

    A first draft of this check used the cap as a hard daily limit. It failed on clean
    data at every review-to-review interval, which is how a check gets ignored.
    """
    if ctx.weights is None or ctx.weights.empty:
        return ctx.ok("max_weight", "range", Severity.BLOCK, "no weights")
    cap = getattr(getattr(ctx.config, "capping", None), "max_single_weight", 0.10)
    drift_allowance = 1.5
    hard_limit = cap * drift_allowance

    largest = float(ctx.weights.max())
    breaching = ctx.weights[ctx.weights > hard_limit]
    if len(breaching):
        return ctx.fail("max_weight", "range", Severity.BLOCK,
                        f"{len(breaching)} constituent(s) above {hard_limit:.1%} "
                        f"({drift_allowance:g}x the {cap:.0%} cap), largest "
                        f"{largest:.2%} - beyond what price drift since the last "
                        "review can explain",
                        n_affected=len(breaching), value=largest, threshold=hard_limit,
                        sample=tuple(str(x) for x in breaching.index[:5]))

    drifted = ctx.weights[ctx.weights > cap + 1e-6]
    if len(drifted):
        return ctx.fail("max_weight", "range", Severity.WARN,
                        f"{len(drifted)} constituent(s) have drifted above the "
                        f"{cap:.0%} cap since the last review, largest {largest:.2%}; "
                        "they will be capped again at the next review",
                        n_affected=len(drifted), value=largest, threshold=cap,
                        sample=tuple(str(x) for x in drifted.index[:5]))

    return ctx.ok("max_weight", "range", Severity.BLOCK,
                  f"largest weight {largest:.4%} within the {cap:.1%} cap",
                  value=largest, threshold=cap)


def check_float_factors(ctx: ValidationContext) -> Finding:
    if ctx.shares is None or "free_float_factor" not in ctx.shares.columns:
        return ctx.ok("float_factor_range", "range", Severity.BLOCK, "no float data")
    f = ctx.shares["free_float_factor"]
    bad = (f < 0) | (f > 1)
    if bad.any():
        return ctx.fail("float_factor_range", "range", Severity.BLOCK,
                        f"{int(bad.sum())} free-float factors outside [0, 1]",
                        n_affected=int(bad.sum()))
    return ctx.ok("float_factor_range", "range", Severity.BLOCK,
                  "all free-float factors in [0, 1]")


def check_divisor_positive(ctx: ValidationContext) -> Finding:
    if ctx.divisor is None:
        return ctx.ok("divisor_positive", "range", Severity.ESCALATE, "no divisor")
    if ctx.divisor <= 0:
        return ctx.fail("divisor_positive", "range", Severity.ESCALATE,
                        f"divisor is {ctx.divisor}: the index is undefined",
                        value=ctx.divisor)
    return ctx.ok("divisor_positive", "range", Severity.ESCALATE,
                  f"divisor {ctx.divisor:,.4f}", value=ctx.divisor)


# --------------------------------------------------------------------------------------
# Cross-field
# --------------------------------------------------------------------------------------


def check_market_value_reconciles(ctx: ValidationContext) -> Finding:
    """Index level times divisor must equal total market value.

    The definitional identity. If it fails, the published level does not correspond to
    the constituents it claims to represent, and nothing downstream can be trusted.
    """
    if None in (ctx.index_level, ctx.divisor, ctx.total_market_value):
        return ctx.ok("market_value_reconciles", "cross_field", Severity.ESCALATE,
                      "insufficient data")
    implied = ctx.index_level * ctx.divisor  # type: ignore[operator]
    actual = ctx.total_market_value
    error = abs(implied - actual) / max(abs(actual), 1e-9)  # type: ignore[arg-type]
    if error > 1e-9:
        return ctx.fail("market_value_reconciles", "cross_field", Severity.ESCALATE,
                        f"level x divisor = {implied:,.2f} but market value is "
                        f"{actual:,.2f} (relative error {error:.2e})",
                        value=error, threshold=1e-9)
    return ctx.ok("market_value_reconciles", "cross_field", Severity.ESCALATE,
                  f"identity holds to {error:.2e}", value=error)


def check_ohlc_consistency(ctx: ValidationContext) -> Finding:
    if ctx.prices is None or not {"high", "low", "close"} <= set(ctx.prices.columns):
        return ctx.ok("ohlc_consistency", "cross_field", Severity.WARN, "no OHLC data")
    p = ctx.prices
    bad = (p["high"] < p["low"]) | (p["close"] > p["high"] * 1.0001) | \
          (p["close"] < p["low"] * 0.9999)
    if bad.any():
        return ctx.fail("ohlc_consistency", "cross_field", Severity.WARN,
                        f"{int(bad.sum())} rows where close sits outside the high-low "
                        "range",
                        n_affected=int(bad.sum()),
                        sample=tuple(p.loc[bad, "security_id"].astype(str).head(5)))
    return ctx.ok("ohlc_consistency", "cross_field", Severity.WARN, "OHLC consistent")


def check_constituent_count(ctx: ValidationContext) -> Finding:
    n = len(ctx.constituents) or (len(ctx.weights) if ctx.weights is not None else 0)
    if n == 0:
        return ctx.fail("constituent_count", "cross_field", Severity.ESCALATE,
                        "the index has no constituents")
    if n < 20:
        return ctx.fail("constituent_count", "cross_field", Severity.BLOCK,
                        f"only {n} constituents: implausibly few for a broad index",
                        value=float(n), threshold=20.0)
    return ctx.ok("constituent_count", "cross_field", Severity.BLOCK,
                  f"{n} constituents", value=float(n))


# --------------------------------------------------------------------------------------
# Temporal
# --------------------------------------------------------------------------------------


def check_price_outliers(ctx: ValidationContext) -> Finding:
    """Cross-sectionally extreme one-day moves, weighted by index impact.

    Compares each security's move to that day's cross-sectional median and median
    absolute deviation, so the threshold adapts to the regime automatically. On a day
    the whole market falls 8%, a security that falls 8% is unremarkable and this rule
    stays quiet - which is precisely when a fixed-sigma rule would fire on everything.
    """
    if ctx.prices is None or ctx.prior_prices is None:
        return ctx.ok("price_outliers", "temporal", Severity.WARN, "no prior prices")

    today = ctx.prices.set_index("security_id")["close"]
    prior = ctx.prior_prices.set_index("security_id")["close"]
    common = today.index.intersection(prior.index)
    if len(common) < 20:
        return ctx.ok("price_outliers", "temporal", Severity.WARN,
                      "too few overlapping securities to test")

    moves = (today[common] / prior[common] - 1.0).dropna()
    median = float(moves.median())
    mad = float((moves - median).abs().median()) or 1e-6
    z = (moves - median).abs() / (1.4826 * mad)

    extreme = z[z > 6.0]
    if extreme.empty:
        return ctx.ok("price_outliers", "temporal", Severity.WARN,
                      f"no move beyond 6 robust sigma (cross-section median "
                      f"{median:+.2%})")

    impact = 0.0
    if ctx.weights is not None:
        impact = float(sum(
            abs(float(ctx.weights.get(s, 0.0)) * float(moves[s])) for s in extreme.index
        ))
    severity = Severity.BLOCK if impact > 0.002 else Severity.WARN
    return Finding(
        rule="price_outliers", category="temporal", severity=severity, passed=False,
        message=(
            f"{len(extreme)} security(ies) moved beyond 6 robust sigma; combined index "
            f"impact {impact * 10_000:.1f}bp"
        ),
        n_affected=len(extreme),
        sample=tuple(f"{s} {moves[s]:+.1%}" for s in extreme.index[:5]),
        value=impact,
    )


def check_stale_prices(ctx: ValidationContext) -> Finding:
    """Prices identical to the previous session.

    Some are legitimate - an illiquid small cap that did not trade. A large number at
    once is a feed that stopped updating, which is the single most common real incident
    and the one most likely to go unnoticed, because a stale index looks calm.
    """
    if ctx.prices is None or ctx.prior_prices is None:
        return ctx.ok("stale_prices", "temporal", Severity.WARN, "no prior prices")
    today = ctx.prices.set_index("security_id")["close"]
    prior = ctx.prior_prices.set_index("security_id")["close"]
    common = today.index.intersection(prior.index)
    if len(common) < 20:
        return ctx.ok("stale_prices", "temporal", Severity.WARN, "too few securities")

    unchanged = (today[common] == prior[common])
    fraction = float(unchanged.mean())
    if fraction > 0.25:
        return ctx.fail("stale_prices", "temporal", Severity.BLOCK,
                        f"{fraction:.0%} of prices are unchanged from the previous "
                        "session, which points at a feed problem rather than a quiet "
                        "market",
                        n_affected=int(unchanged.sum()), value=fraction, threshold=0.25,
                        sample=tuple(str(s) for s in unchanged[unchanged].index[:5]))
    if fraction > 0.10:
        return ctx.fail("stale_prices", "temporal", Severity.WARN,
                        f"{fraction:.0%} of prices unchanged from the previous session",
                        n_affected=int(unchanged.sum()), value=fraction)
    return ctx.ok("stale_prices", "temporal", Severity.WARN,
                  f"{fraction:.1%} unchanged, within normal range", value=fraction)


def check_index_level_move(ctx: ValidationContext) -> Finding:
    if ctx.index_level is None or ctx.prior_index_level in (None, 0):
        return ctx.ok("index_level_move", "temporal", Severity.BLOCK, "no prior level")
    move = ctx.index_level / ctx.prior_index_level - 1.0  # type: ignore[operator]
    if abs(move) > 0.15:
        return ctx.fail("index_level_move", "temporal", Severity.ESCALATE,
                        f"index moved {move:+.2%} in one session - verify before "
                        "publication",
                        value=move, threshold=0.15)
    if abs(move) > 0.07:
        return ctx.fail("index_level_move", "temporal", Severity.WARN,
                        f"index moved {move:+.2%} in one session", value=move)
    return ctx.ok("index_level_move", "temporal", Severity.WARN,
                  f"index moved {move:+.2%}", value=move)


def check_divisor_continuity(ctx: ValidationContext) -> Finding:
    """Divisor events must leave the level continuous.

    The most important check in the suite. A non-zero continuity error means an event
    was misclassified or the rebase used the wrong baseline, and the published level is
    simply wrong.
    """
    if ctx.divisor_audit is None or ctx.divisor_audit.empty:
        return ctx.ok("divisor_continuity", "temporal", Severity.ESCALATE,
                      "no divisor events today")
    audit = ctx.divisor_audit
    if "continuity_error_bps" not in audit.columns:
        return ctx.ok("divisor_continuity", "temporal", Severity.ESCALATE,
                      "audit trail has no continuity column")
    breaches = audit[audit["continuity_error_bps"].abs() > 1.0]
    if not breaches.empty:
        return ctx.fail("divisor_continuity", "temporal", Severity.ESCALATE,
                        f"{len(breaches)} divisor event(s) moved the index level when "
                        "they should not have",
                        n_affected=len(breaches),
                        sample=tuple(
                            f"{r.event_type}/{r.security_id} "
                            f"{r.continuity_error_bps:+.2f}bp"
                            for r in breaches.head(5).itertuples(index=False)),
                        value=float(breaches["continuity_error_bps"].abs().max()),
                        threshold=1.0)
    return ctx.ok("divisor_continuity", "temporal", Severity.ESCALATE,
                  f"{len(audit)} divisor event(s), all continuous")


def check_divisor_jump(ctx: ValidationContext) -> Finding:
    if ctx.divisor is None or ctx.prior_divisor in (None, 0):
        return ctx.ok("divisor_jump", "temporal", Severity.WARN, "no prior divisor")
    change = ctx.divisor / ctx.prior_divisor - 1.0  # type: ignore[operator]
    if abs(change) > 0.05:
        return ctx.fail("divisor_jump", "temporal", Severity.BLOCK,
                        f"divisor changed {change:+.2%} in one session; expect a "
                        "review or a large corporate action to explain it",
                        value=change, threshold=0.05)
    return ctx.ok("divisor_jump", "temporal", Severity.WARN,
                  f"divisor changed {change:+.4%}", value=change)


def check_fx_sanity(ctx: ValidationContext) -> Finding:
    """FX rates positive and finite. A necessary check, and nowhere near sufficient."""
    if ctx.fx is None or ctx.fx.empty:
        return ctx.ok("fx_sanity", "range", Severity.BLOCK, "no FX data")
    bad = ctx.fx[(ctx.fx["rate"] <= 0) | ~np.isfinite(ctx.fx["rate"])]
    if not bad.empty:
        return ctx.fail("fx_sanity", "range", Severity.BLOCK,
                        f"{len(bad)} FX rate(s) non-positive or non-finite",
                        n_affected=len(bad),
                        sample=tuple(f"{r.quote}={r.rate:g}"
                                     for r in bad.head(5).itertuples(index=False)))
    return ctx.ok("fx_sanity", "range", Severity.BLOCK,
                  f"{len(ctx.fx)} FX rates positive and finite")


def check_fx_continuity(ctx: ValidationContext) -> Finding:
    """FX rates against yesterday's, which is what actually catches an inverted rate.

    Added after a chaos drill: an inverted SEK rate of 9.27 sailed through the
    plausible-range check, because 9.27 *is* a plausible number for some currency pair.
    A static range test cannot catch it. The rate's own history can - an inversion is a
    ~99% or ~8500% one-day move, which no floating currency does.

    This is the general lesson about validating market data: absolute bounds catch
    corruption, and only comparison against history catches a wrong-but-valid value.
    """
    if ctx.fx is None or ctx.fx.empty or ctx.prior_fx is None or ctx.prior_fx.empty:
        return ctx.ok("fx_continuity", "temporal", Severity.BLOCK,
                      "no prior FX rates to compare against")

    today = ctx.fx.set_index("quote")["rate"]
    prior = ctx.prior_fx.set_index("quote")["rate"]
    common = today.index.intersection(prior.index)
    if len(common) == 0:
        return ctx.ok("fx_continuity", "temporal", Severity.BLOCK, "no overlap")

    moves = (today[common] / prior[common] - 1.0).abs()
    extreme = moves[moves > 0.10]
    if not extreme.empty:
        inverted = [
            str(c) for c in extreme.index
            if abs(today[c] * prior[c] - 1.0) < 0.25
        ]
        message = (
            f"{len(extreme)} FX rate(s) moved more than 10% in one session"
            + (f"; {', '.join(inverted)} look INVERTED (rate x prior rate is close "
               "to 1)" if inverted else "")
        )
        return ctx.fail("fx_continuity", "temporal", Severity.ESCALATE, message,
                        n_affected=len(extreme),
                        sample=tuple(f"{c} {moves[c]:+.1%}" for c in extreme.index[:5]),
                        value=float(moves.max()), threshold=0.10)
    return ctx.ok("fx_continuity", "temporal", Severity.BLOCK,
                  f"all {len(common)} rates moved less than 10%")


def check_all_constituents_priced(ctx: ValidationContext) -> Finding:
    """Every weighted constituent must have a price today.

    Added after a chaos drill: a delisting processed in the reference feed but not in
    the index left a name carrying weight with no price row, and nothing noticed. The
    index then values it at its last known price indefinitely - which is exactly the
    "suspended three weeks ago" scenario, except unintended and unmonitored.
    """
    if ctx.weights is None or ctx.weights.empty or ctx.prices is None:
        return ctx.ok("constituents_priced", "cross_field", Severity.BLOCK,
                      "no weights or prices")
    priced = set(ctx.prices["security_id"].astype(str))
    unpriced = [s for s in ctx.weights.index.astype(str) if s not in priced]
    if unpriced:
        lost_weight = float(ctx.weights.reindex(unpriced).sum())
        severity = Severity.ESCALATE if lost_weight > 0.005 else Severity.BLOCK
        return Finding(
            rule="constituents_priced", category="cross_field", severity=severity,
            passed=False,
            message=(
                f"{len(unpriced)} constituent(s) carrying {lost_weight:.2%} of index "
                "weight have no price today"
            ),
            n_affected=len(unpriced), sample=tuple(unpriced[:5]), value=lost_weight,
        )
    return ctx.ok("constituents_priced", "cross_field", Severity.BLOCK,
                  f"all {len(ctx.weights)} constituents priced")


# --------------------------------------------------------------------------------------
# Cross-source and reconciliation
# --------------------------------------------------------------------------------------


def check_cross_source_prices(ctx: ValidationContext) -> Finding:
    """Compare the primary feed against a second source.

    The only check that catches a *systematically* wrong primary feed. Everything else
    validates the data against itself, which cannot detect an error that is internally
    consistent.
    """
    if ctx.alternate_source is None or ctx.prices is None:
        return ctx.ok("cross_source_prices", "cross_source", Severity.WARN,
                      "no second source configured")
    primary = ctx.prices.set_index("security_id")["close"]
    alt = ctx.alternate_source.set_index("security_id")["close"]
    common = primary.index.intersection(alt.index)
    if len(common) < 10:
        return ctx.ok("cross_source_prices", "cross_source", Severity.WARN,
                      "too little overlap to compare")
    diff = (primary[common] / alt[common] - 1.0).abs()
    disagree = diff[diff > 0.005]
    if not disagree.empty:
        return ctx.fail("cross_source_prices", "cross_source", Severity.WARN,
                        f"{len(disagree)} securities differ by more than 50bp between "
                        "sources",
                        n_affected=len(disagree),
                        sample=tuple(f"{s} {diff[s]:.2%}" for s in disagree.index[:5]),
                        value=float(diff.max()))
    return ctx.ok("cross_source_prices", "cross_source", Severity.WARN,
                  f"{len(common)} securities agree within 50bp")


def check_reconciliation(ctx: ValidationContext) -> Finding:
    """Our level against the officially published one."""
    if ctx.official_level is None or ctx.index_level is None:
        return ctx.ok("reconciliation", "reconciliation", Severity.BLOCK,
                      "no official level to reconcile against")
    error = abs(ctx.index_level / ctx.official_level - 1.0) * 10_000
    if error > 1.0:
        return ctx.fail("reconciliation", "reconciliation", Severity.BLOCK,
                        f"calculated level differs from official by {error:.2f}bp",
                        value=error, threshold=1.0)
    return ctx.ok("reconciliation", "reconciliation", Severity.BLOCK,
                  f"reconciles to within {error:.3f}bp", value=error)


def check_corporate_actions_applied(ctx: ValidationContext) -> Finding:
    """Every corporate action with today's ex-date must appear in the divisor audit.

    Catches the missed corporate action - a dividend that was in the feed and never
    applied. The index is then wrong by the dividend, quietly, and only shows up when a
    tracking fund reconciles.
    """
    if ctx.corp_actions is None or ctx.corp_actions.empty:
        return ctx.ok("corp_actions_applied", "cross_field", Severity.BLOCK,
                      "no corporate actions today")
    due = ctx.corp_actions[ctx.corp_actions["ex_date"] == ctx.as_of]
    if due.empty:
        return ctx.ok("corp_actions_applied", "cross_field", Severity.BLOCK,
                      "no corporate actions with today's ex-date")
    if ctx.divisor_audit is None or ctx.divisor_audit.empty:
        return ctx.fail("corp_actions_applied", "cross_field", Severity.BLOCK,
                        f"{len(due)} corporate action(s) due today but the audit trail "
                        "is empty",
                        n_affected=len(due))

    applied = set(ctx.divisor_audit["event_id"]) if "event_id" in \
        ctx.divisor_audit.columns else set()
    constituents = set(ctx.constituents) or set(
        ctx.weights.index if ctx.weights is not None else [])
    relevant = due[due["security_id"].isin(constituents)] if constituents else due
    missing = relevant[~relevant["event_id"].isin(applied)]
    if not missing.empty:
        return ctx.fail("corp_actions_applied", "cross_field", Severity.ESCALATE,
                        f"{len(missing)} corporate action(s) on constituents were not "
                        "applied",
                        n_affected=len(missing),
                        sample=tuple(missing["event_id"].astype(str).head(5)))
    return ctx.ok("corp_actions_applied", "cross_field", Severity.BLOCK,
                  f"all {len(relevant)} constituent corporate action(s) applied")


def check_shares_plausible(ctx: ValidationContext) -> Finding:
    if ctx.shares is None or "shares_outstanding" not in ctx.shares.columns:
        return ctx.ok("shares_plausible", "range", Severity.WARN, "no share data")
    s = ctx.shares["shares_outstanding"]
    bad = (s <= 0) | ~np.isfinite(s)
    if bad.any():
        return ctx.fail("shares_plausible", "range", Severity.BLOCK,
                        f"{int(bad.sum())} non-positive or non-finite share counts",
                        n_affected=int(bad.sum()))
    return ctx.ok("shares_plausible", "range", Severity.WARN,
                  f"{len(s)} share counts positive and finite")


def check_weight_concentration(ctx: ValidationContext) -> Finding:
    """Top-ten concentration as a drift monitor.

    Not a rule breach on its own - concentration is a market fact, and it has risen
    genuinely. The point is to notice a *step change*, which is usually a capping
    failure rather than a market move.
    """
    if ctx.weights is None or len(ctx.weights) < 10:
        return ctx.ok("weight_concentration", "aggregate", Severity.INFO,
                      "too few constituents")
    top10 = float(ctx.weights.nlargest(10).sum())
    if top10 > 0.50:
        return ctx.fail("weight_concentration", "aggregate", Severity.WARN,
                        f"top ten constituents are {top10:.1%} of the index",
                        value=top10, threshold=0.50)
    return ctx.ok("weight_concentration", "aggregate", Severity.INFO,
                  f"top ten are {top10:.1%}", value=top10)


def check_currency_coverage(ctx: ValidationContext) -> Finding:
    if ctx.prices is None or ctx.fx is None or ctx.fx.empty:
        return ctx.ok("currency_coverage", "cross_source", Severity.BLOCK,
                      "no FX or price data")
    needed = set(ctx.prices["currency"].astype(str).unique())
    have = set(ctx.fx["quote"].astype(str).unique()) | {"USD"}
    missing = needed - have
    if missing:
        return ctx.fail("currency_coverage", "cross_source", Severity.BLOCK,
                        f"no FX rate for {sorted(missing)}",
                        n_affected=len(missing),
                        sample=tuple(sorted(missing)[:5]))
    return ctx.ok("currency_coverage", "cross_source", Severity.BLOCK,
                  f"all {len(needed)} currencies have rates")


# --------------------------------------------------------------------------------------

DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("prices_present", "schema", Severity.ESCALATE, check_prices_present,
         description="Price data exists for the run date."),
    Rule("required_columns", "schema", Severity.BLOCK, check_required_columns,
         description="The price frame has every column the engine needs."),
    Rule("no_duplicate_prices", "schema", Severity.BLOCK, check_no_duplicate_prices,
         description="No duplicate (security, date) rows."),
    Rule("no_null_prices", "schema", Severity.BLOCK, check_no_null_prices,
         description="No null closing prices."),
    Rule("positive_prices", "range", Severity.BLOCK, check_positive_prices,
         description="Every price is strictly positive."),
    Rule("weights_sum", "range", Severity.BLOCK, check_weights_sum_to_one,
         description="Constituent weights sum to one."),
    Rule("weights_non_negative", "range", Severity.BLOCK, check_weights_non_negative,
         description="No negative weights."),
    Rule("max_weight", "range", Severity.BLOCK, check_max_weight,
         description="No constituent exceeds the published concentration cap."),
    Rule("float_factor_range", "range", Severity.BLOCK, check_float_factors,
         description="Free-float factors lie in [0, 1]."),
    Rule("divisor_positive", "range", Severity.ESCALATE, check_divisor_positive,
         description="The divisor is strictly positive."),
    Rule("shares_plausible", "range", Severity.WARN, check_shares_plausible,
         description="Share counts are positive and finite."),
    Rule("fx_sanity", "range", Severity.BLOCK, check_fx_sanity,
         description="FX rates are positive and finite."),
    Rule("fx_continuity", "temporal", Severity.ESCALATE, check_fx_continuity,
         description="FX rates did not move implausibly since the previous session; "
                     "detects an inverted rate, which a range check cannot."),
    Rule("constituents_priced", "cross_field", Severity.BLOCK,
         check_all_constituents_priced,
         description="Every weighted constituent has a price today."),
    Rule("market_value_reconciles", "cross_field", Severity.ESCALATE,
         check_market_value_reconciles,
         description="Level x divisor equals total investable market value."),
    Rule("ohlc_consistency", "cross_field", Severity.WARN, check_ohlc_consistency,
         description="Close lies within the day's high-low range."),
    Rule("constituent_count", "cross_field", Severity.BLOCK, check_constituent_count,
         description="The constituent count is plausible."),
    Rule("corp_actions_applied", "cross_field", Severity.ESCALATE,
         check_corporate_actions_applied,
         description="Every corporate action due today was applied."),
    Rule("price_outliers", "temporal", Severity.WARN, check_price_outliers,
         description="No cross-sectionally extreme price move with material index "
                     "impact."),
    Rule("stale_prices", "temporal", Severity.BLOCK, check_stale_prices,
         description="Not an implausible share of prices unchanged since yesterday."),
    Rule("index_level_move", "temporal", Severity.WARN, check_index_level_move,
         description="The index level move is within a plausible daily range."),
    Rule("divisor_continuity", "temporal", Severity.ESCALATE, check_divisor_continuity,
         description="Divisor events leave the index level continuous."),
    Rule("divisor_jump", "temporal", Severity.WARN, check_divisor_jump,
         description="The divisor did not move implausibly in one session."),
    Rule("cross_source_prices", "cross_source", Severity.WARN, check_cross_source_prices,
         description="The primary price feed agrees with a second source."),
    Rule("currency_coverage", "cross_source", Severity.BLOCK, check_currency_coverage,
         description="Every constituent currency has an FX rate."),
    Rule("weight_concentration", "aggregate", Severity.INFO, check_weight_concentration,
         description="Top-ten concentration is monitored for step changes."),
    Rule("reconciliation", "reconciliation", Severity.BLOCK, check_reconciliation,
         description="The calculated level reconciles with the official one."),
)


def rules_by_category() -> pd.DataFrame:
    return (
        pd.DataFrame([{"category": r.category, "severity": r.severity.name}
                      for r in DEFAULT_RULES])
        .groupby(["category", "severity"], as_index=False).size()
        .rename(columns={"size": "n_rules"})
    )
