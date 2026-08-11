"""Fault injection: the chaos drill.

Ten realistic data defects, injected into clean data, so the validation suite can be
measured rather than assumed. A rules engine nobody has tried to defeat is a rules
engine of unknown value, and the useful output of this module is not the faults it
catches - it is the list of faults it *misses*.

Each fault is drawn from something that actually happens: a decimal error in a manual
override, a dividend that failed to load, a share count applied a day late, two
identifiers swapped in a mapping file, an FX rate stored inverted, a split applied
twice by two different jobs.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from miniftse.quality.rules import ValidationContext, ValidationEngine

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from miniftse.production.build import BuildResult


@dataclass(frozen=True, slots=True)
class Fault:
    """A single injected defect."""

    fault_id: str
    name: str
    description: str
    realism: str
    """Where this comes from in the real world. Included because a drill against
    unrealistic faults trains the wrong reflexes."""

    apply: Callable[[ValidationContext, np.random.Generator], InjectionRecord]
    expected_detector: str
    """Which rule *should* catch it. Comparing this to what actually fires is the
    coverage gap."""


@dataclass
class InjectionRecord:
    context: ValidationContext
    affected: tuple[str, ...]
    detail: str


def _copy(ctx: ValidationContext) -> ValidationContext:
    from dataclasses import replace

    return replace(
        ctx,
        prices=ctx.prices.copy() if ctx.prices is not None else None,
        prior_prices=ctx.prior_prices.copy() if ctx.prior_prices is not None else None,
        shares=ctx.shares.copy() if ctx.shares is not None else None,
        corp_actions=ctx.corp_actions.copy() if ctx.corp_actions is not None else None,
        weights=ctx.weights.copy() if ctx.weights is not None else None,
        fx=ctx.fx.copy() if ctx.fx is not None else None,
        prior_fx=ctx.prior_fx.copy() if ctx.prior_fx is not None else None,
        divisor_audit=(ctx.divisor_audit.copy()
                       if ctx.divisor_audit is not None else None),
    )


# --------------------------------------------------------------------------------------


def fault_price_factor_ten(ctx: ValidationContext, rng: np.random.Generator
                           ) -> InjectionRecord:
    """A price off by a factor of ten - the classic decimal error."""
    c = _copy(ctx)
    assert c.prices is not None
    i = int(rng.integers(0, len(c.prices)))
    sec = str(c.prices.iloc[i]["security_id"])
    c.prices.iloc[i, c.prices.columns.get_loc("close")] *= 10.0
    return InjectionRecord(c, (sec,), f"{sec} price multiplied by 10")


def fault_missing_dividend(ctx: ValidationContext, rng: np.random.Generator
                           ) -> InjectionRecord:
    """A dividend in the feed that never reached the divisor audit."""
    c = _copy(ctx)
    if c.divisor_audit is None or c.divisor_audit.empty:
        return InjectionRecord(c, (), "no divisor audit to strip")
    audit = c.divisor_audit
    # Must strip a dividend with TODAY's ex-date. Removing one from three years ago
    # tests nothing: the corporate-actions check only compares against events due
    # today, so an old row is invisible to it either way.
    if "date" in audit.columns:
        audit = audit[audit["date"] == c.as_of]
    dividends = audit[audit["event_type"].astype(str).str.contains("DIVIDEND")]
    if dividends.empty:
        return InjectionRecord(c, (), "no dividend events with today's ex-date")
    drop = dividends.index[0]
    sec = str(c.divisor_audit.loc[drop, "security_id"])
    c.divisor_audit = c.divisor_audit.drop(index=drop)
    return InjectionRecord(c, (sec,), f"dividend on {sec} removed from the audit trail")


def fault_stale_feed(ctx: ValidationContext, rng: np.random.Generator) -> InjectionRecord:
    """A third of the feed stopped updating - prices held from yesterday."""
    c = _copy(ctx)
    assert c.prices is not None and c.prior_prices is not None
    prior = c.prior_prices.set_index("security_id")["close"]
    n = max(1, int(len(c.prices) * 0.33))
    rows = rng.choice(len(c.prices), size=n, replace=False)
    affected = []
    for i in rows:
        sec = str(c.prices.iloc[i]["security_id"])
        if sec in prior.index:
            c.prices.iloc[i, c.prices.columns.get_loc("close")] = float(prior[sec])
            affected.append(sec)
    return InjectionRecord(c, tuple(affected[:5]),
                           f"{len(affected)} prices held at yesterday's close")


def fault_swapped_identifiers(ctx: ValidationContext, rng: np.random.Generator
                              ) -> InjectionRecord:
    """Two securities' prices swapped - a mapping file with two rows transposed."""
    c = _copy(ctx)
    assert c.prices is not None
    if len(c.prices) < 2:
        return InjectionRecord(c, (), "too few rows")
    i, j = rng.choice(len(c.prices), size=2, replace=False)
    col = c.prices.columns.get_loc("close")
    a, b = str(c.prices.iloc[i]["security_id"]), str(c.prices.iloc[j]["security_id"])
    va, vb = c.prices.iloc[i, col], c.prices.iloc[j, col]
    c.prices.iloc[i, col], c.prices.iloc[j, col] = vb, va
    return InjectionRecord(c, (a, b), f"prices for {a} and {b} transposed")


def fault_inverted_fx(ctx: ValidationContext, rng: np.random.Generator) -> InjectionRecord:
    """An FX rate stored as the reciprocal.

    Nasty because the value is perfectly valid. It affects only one currency's
    constituents and looks exactly like a country factor return.
    """
    c = _copy(ctx)
    if c.fx is None or c.fx.empty:
        return InjectionRecord(c, (), "no FX data")
    non_base = c.fx[c.fx["quote"] != "USD"]
    if non_base.empty:
        return InjectionRecord(c, (), "no foreign currencies")
    idx = non_base.index[int(rng.integers(0, len(non_base)))]
    ccy = str(c.fx.loc[idx, "quote"])
    rate = float(c.fx.loc[idx, "rate"])
    c.fx.loc[idx, "rate"] = 1.0 / rate if rate else 0.0
    return InjectionRecord(c, (ccy,), f"{ccy} rate inverted ({rate:.4f} -> {1/rate:.4f})")


def fault_double_split(ctx: ValidationContext, rng: np.random.Generator
                       ) -> InjectionRecord:
    """A split applied twice - two jobs both processing the same event file."""
    c = _copy(ctx)
    assert c.prices is not None
    i = int(rng.integers(0, len(c.prices)))
    sec = str(c.prices.iloc[i]["security_id"])
    c.prices.iloc[i, c.prices.columns.get_loc("close")] /= 2.0
    return InjectionRecord(c, (sec,), f"{sec} halved by a duplicate split")


def fault_negative_price(ctx: ValidationContext, rng: np.random.Generator
                         ) -> InjectionRecord:
    """A negative price - a sign error in a manual correction."""
    c = _copy(ctx)
    assert c.prices is not None
    i = int(rng.integers(0, len(c.prices)))
    sec = str(c.prices.iloc[i]["security_id"])
    c.prices.iloc[i, c.prices.columns.get_loc("close")] *= -1.0
    return InjectionRecord(c, (sec,), f"{sec} price sign flipped")


def fault_weights_dont_sum(ctx: ValidationContext, rng: np.random.Generator
                           ) -> InjectionRecord:
    """Weights that no longer sum to one - a constituent dropped after normalisation."""
    c = _copy(ctx)
    if c.weights is None or c.weights.empty:
        return InjectionRecord(c, (), "no weights")
    victim = str(c.weights.index[int(rng.integers(0, len(c.weights)))])
    c.weights = c.weights.drop(index=victim)
    return InjectionRecord(c, (victim,),
                           f"{victim} removed from the weight vector after normalisation")


def fault_cap_breach(ctx: ValidationContext, rng: np.random.Generator) -> InjectionRecord:
    """A constituent above the published cap - capping silently failed to converge."""
    c = _copy(ctx)
    if c.weights is None or c.weights.empty:
        return InjectionRecord(c, (), "no weights")
    victim = str(c.weights.idxmax())
    c.weights = c.weights.copy()
    c.weights[victim] = 0.18
    c.weights = c.weights / c.weights.sum()
    return InjectionRecord(c, (victim,), f"{victim} pushed to {c.weights[victim]:.1%}")


def fault_duplicate_row(ctx: ValidationContext, rng: np.random.Generator
                        ) -> InjectionRecord:
    """A duplicated price row - a file loaded twice."""
    c = _copy(ctx)
    assert c.prices is not None
    i = int(rng.integers(0, len(c.prices)))
    sec = str(c.prices.iloc[i]["security_id"])
    c.prices = pd.concat([c.prices, c.prices.iloc[[i]]], ignore_index=True)
    return InjectionRecord(c, (sec,), f"price row for {sec} duplicated")


def fault_delisted_still_held(ctx: ValidationContext, rng: np.random.Generator
                              ) -> InjectionRecord:
    """A constituent whose price stopped arriving but which never left the index."""
    c = _copy(ctx)
    if c.weights is None or c.prices is None or c.weights.empty:
        return InjectionRecord(c, (), "no weights")
    victim = str(c.weights.index[int(rng.integers(0, len(c.weights)))])
    c.prices = c.prices[c.prices["security_id"] != victim]
    return InjectionRecord(c, (victim,),
                           f"{victim} still weighted but has no price row")


def fault_shares_wrong_sign(ctx: ValidationContext, rng: np.random.Generator
                            ) -> InjectionRecord:
    """A negative share count from a badly parsed fundamental file."""
    c = _copy(ctx)
    if c.shares is None or c.shares.empty:
        return InjectionRecord(c, (), "no share data")
    i = int(rng.integers(0, len(c.shares)))
    sec = str(c.shares.iloc[i]["security_id"])
    col = c.shares.columns.get_loc("shares_outstanding")
    c.shares.iloc[i, col] *= -1.0
    return InjectionRecord(c, (sec,), f"{sec} share count sign flipped")


FAULTS: tuple[Fault, ...] = (
    Fault("F01", "price off by 10x", "A single close multiplied by ten.",
          "Decimal error in a manual price override, or a vendor sending pence for "
          "pounds.", fault_price_factor_ten, "price_outliers"),
    Fault("F02", "missing dividend", "A dividend present in the feed but never applied.",
          "The corporate action file arrived after the calculation job started.",
          fault_missing_dividend, "corp_actions_applied"),
    Fault("F03", "stale feed", "A third of prices held at yesterday's close.",
          "A regional feed stopped publishing and the job carried the last value "
          "forward.", fault_stale_feed, "stale_prices"),
    Fault("F04", "swapped identifiers", "Two securities' prices transposed.",
          "Two rows swapped in a mapping file after a SEDOL change.",
          fault_swapped_identifiers, "price_outliers"),
    Fault("F05", "inverted FX rate", "One currency stored as its reciprocal.",
          "A new currency onboarded with the quote convention the wrong way round.",
          fault_inverted_fx, "fx_continuity"),
    Fault("F06", "double-applied split", "A split applied twice.",
          "Two jobs processing the same corporate action file after a retry.",
          fault_double_split, "price_outliers"),
    Fault("F07", "negative price", "A price with the sign flipped.",
          "A sign error in a manual correction.", fault_negative_price,
          "positive_prices"),
    Fault("F08", "weights do not sum", "A constituent dropped after normalisation.",
          "A filter applied downstream of the weighting step.",
          fault_weights_dont_sum, "weights_sum"),
    Fault("F09", "cap breach", "A constituent above the published cap.",
          "Iterative capping hit its iteration limit and returned anyway.",
          fault_cap_breach, "max_weight"),
    Fault("F10", "duplicate row", "A price row present twice.",
          "A daily file loaded twice after a failed run was retried.",
          fault_duplicate_row, "no_duplicate_prices"),
    Fault("F11", "delisted but still held", "A weighted constituent with no price.",
          "A delisting processed in the reference feed but not in the index.",
          fault_delisted_still_held, "constituents_priced"),
    Fault("F12", "negative share count", "A share count with the sign flipped.",
          "A badly parsed fundamental file where brackets meant negative.",
          fault_shares_wrong_sign, "shares_plausible"),
)


# --------------------------------------------------------------------------------------


@dataclass
class DrillResult:
    fault_id: str
    fault_name: str
    detected: bool
    detected_by: tuple[str, ...]
    expected_detector: str
    caught_by_expected: bool
    highest_severity: str
    blocked_publication: bool
    detail: str


def run_chaos_drill(
    baseline: ValidationContext,
    engine: ValidationEngine | None = None,
    seed: int = 20260809,
    faults: tuple[Fault, ...] = FAULTS,
) -> tuple[pd.DataFrame, list[str]]:
    """Inject each fault into a clean context and report what the rules caught.

    Returns the per-fault results and the list of coverage gaps. The gaps are the
    output that matters: they are the checks that do not exist yet, and finding them
    this way is far cheaper than finding them in production.
    """
    engine = engine or ValidationEngine.default()
    rng = np.random.default_rng(seed)

    clean = engine.run(baseline, "baseline")
    # Baseline failures are subtracted so the drill cannot take credit for noise. But
    # a rule already failing at WARN that now fails at BLOCK *has* detected the fault,
    # and so has one whose affected count jumps. Comparing rule names alone scored
    # three genuine detections as misses - the cap breach and the unpriced constituent
    # were both caught, by rules that were already grumbling about ordinary drift.
    baseline_state = {
        f.rule: (f.severity.value, f.n_affected) for f in clean.failures
    }

    results: list[DrillResult] = []
    for fault in faults:
        record = fault.apply(baseline, rng)
        if not record.affected:
            # The injector could not place this fault in the chosen cross-section -
            # no dividend with today's ex-date, for instance. Not a coverage gap;
            # reported separately so it cannot masquerade as one.
            results.append(DrillResult(
                fault_id=fault.fault_id, fault_name=fault.name, detected=False,
                detected_by=(), expected_detector=fault.expected_detector,
                caught_by_expected=False, highest_severity="n/a",
                blocked_publication=False,
                detail=f"NOT INJECTED - {record.detail}",
            ))
            continue

        report = engine.run(record.context, f"drill-{fault.fault_id}")
        triggered = tuple(
            f.rule for f in report.failures
            if f.rule not in baseline_state
            or f.severity.value > baseline_state[f.rule][0]
            or f.n_affected > baseline_state[f.rule][1]
        )
        severities = [f.severity for f in report.failures if f.rule in triggered]
        results.append(DrillResult(
            fault_id=fault.fault_id, fault_name=fault.name,
            detected=bool(triggered), detected_by=triggered,
            expected_detector=fault.expected_detector,
            caught_by_expected=fault.expected_detector in triggered,
            highest_severity=(max(severities, key=lambda s: s.value).name
                              if severities else "-"),
            blocked_publication=not report.may_publish,
            detail=record.detail,
        ))

    frame = pd.DataFrame([r.__dict__ for r in results])
    frame["detected_by"] = frame["detected_by"].apply(lambda t: ", ".join(t))

    gaps = [
        f"{r.fault_id} ({r.fault_name}): {r.detail} - NOT DETECTED by any rule"
        for r in results if not r.detected and not r.detail.startswith("NOT INJECTED")
    ] + [
        f"{r.fault_id} ({r.fault_name}): caught, but by "
        f"{', '.join(r.detected_by)} rather than the expected "
        f"'{r.expected_detector}' - the intended check has a blind spot"
        for r in results if r.detected and not r.caught_by_expected
    ]
    return frame, gaps


def drill_summary(frame: pd.DataFrame) -> str:
    injected = frame[~frame["detail"].str.startswith("NOT INJECTED")]
    n = len(injected)
    skipped = len(frame) - n
    detected = int(injected["detected"].sum())
    blocked = int(injected["blocked_publication"].sum())
    as_expected = int(injected["caught_by_expected"].sum())
    tail = f" ({skipped} not injectable in this cross-section)" if skipped else ""
    return (
        f"{detected}/{n} injected faults detected, {blocked}/{n} blocked publication, "
        f"{as_expected}/{n} caught by the intended rule{tail}."
    )


def build_baseline_context(
    prices: pd.DataFrame,
    prior_prices: pd.DataFrame,
    weights: pd.Series,
    shares: pd.DataFrame,
    fx: pd.DataFrame,
    as_of: dt.date,
    divisor: float,
    index_level: float,
    total_market_value: float,
    divisor_audit: pd.DataFrame | None = None,
    corp_actions: pd.DataFrame | None = None,
    prior_fx: pd.DataFrame | None = None,
    config: Any = None,
    prior_index_level: float | None = None,
    prior_divisor: float | None = None,
) -> ValidationContext:
    """A clean context for the drill to corrupt."""
    return ValidationContext(
        as_of=as_of, prices=prices, prior_prices=prior_prices, weights=weights,
        shares=shares, fx=fx, divisor=divisor, index_level=index_level,
        total_market_value=total_market_value, divisor_audit=divisor_audit,
        corp_actions=corp_actions, prior_fx=prior_fx, config=config,
        prior_index_level=prior_index_level, prior_divisor=prior_divisor,
        constituents=dict.fromkeys(weights.index, None),
    )


def _config_from_manifest(config_dict: dict[str, Any]) -> Any:
    """The named `IndexConfig` whose serialised form the run manifest recorded.

    `RunManifest.config` is `IndexConfig.to_dict()` output - a dict, and for any real
    build a non-empty, truthy one - never an `IndexConfig`. An earlier version of
    `baseline_from_build` wrote `result.manifest.config and global_all_cap()`, which
    therefore always evaluated to `global_all_cap()` whatever config the build used:
    the right answer for every caller in this repository (the CLI drill and the desk
    snapshot both build the default global all-cap spec), but by accident, not by
    decision. Matching the recorded dict against the named constructors keeps that
    behaviour for those callers and stops silently mislabelling any other build's
    baseline - a `global_large_mid` drill would otherwise report itself validated
    against the wrong index's config.

    The constructor set is `ValidationContext._CONFIG_CONSTRUCTORS` - the same closed
    set `save()`/`load()` round-trip a config name through, kept as the single source
    of truth rather than restated here. A dict no named constructor produces raises
    for the same reason `ValidationContext._config_name` does: it cannot be identified
    by name, and guessing would be worse than stopping.
    """
    for ctor in ValidationContext._CONFIG_CONSTRUCTORS.values():
        candidate = ctor()
        if candidate.to_dict() == config_dict:
            return candidate
    raise ValueError(
        "the run manifest's config matches no named constructor in miniftse.config "
        "(global_all_cap, global_large_mid, developed_only), so the drill baseline "
        "cannot name the config it validated against. If configs are ever built "
        "inline, extend ValidationContext's save()/load() to carry config.to_dict() "
        "instead of a name first."
    )


def baseline_from_build(result: BuildResult) -> ValidationContext:
    """The clean context for a drill, taken from the final day of a completed build.

    This assembly used to sit inline in `cli.chaos_drill_cmd`. It lives here because it
    reaches into `SyntheticUniverse._generated` and stitches together nine separate
    pieces of a `BuildResult` - library knowledge with no business being duplicated in a
    CLI command and again in a web-facing precompute step. `cli.chaos_drill_cmd` and
    `desk.snapshot.build_snapshot` are both callers, and there is one definition of what
    "the baseline" means.
    """
    history, universe = result.history, result.universe
    last = history.levels.iloc[-1]
    prior = history.levels.iloc[-2]
    as_of = last["date"]

    prices = universe._generated["prices"]
    today = prices[prices["date"] == as_of]
    prior_dates = sorted(d for d in prices["date"].unique() if d < as_of)
    yesterday = prices[prices["date"] == prior_dates[-1]]

    snapshot = history.weights[history.weights["date"] == history.weights["date"].max()]
    weights = snapshot.set_index("security_id")["weight"]

    quotes = list(universe._fx["quote"].unique())
    return build_baseline_context(
        prices=today, prior_prices=yesterday, weights=weights,
        shares=universe.get_shares(None, as_of),
        fx=universe.get_fx("USD", quotes, as_of, as_of),
        prior_fx=universe.get_fx("USD", quotes, prior_dates[-1], prior_dates[-1]),
        as_of=as_of, divisor=float(last["divisor"]),
        index_level=float(last["price_return"]),
        total_market_value=float(last["total_market_value"]),
        divisor_audit=result.calculator.engine.audit_frame(),
        corp_actions=universe.get_corp_actions(None, as_of, as_of),
        config=_config_from_manifest(result.manifest.config),
        prior_index_level=float(prior["price_return"]),
        prior_divisor=float(prior["divisor"]),
    )
