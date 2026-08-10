"""Dagster definitions for daily index production.

`production.pipeline` is a hand-rolled DAG and is deliberately kept: it has no external
dependency, so the core package stays installable anywhere and the tests run in
milliseconds. This module wraps the *same* steps as a Dagster job, because in production
the scheduling, retries, backfills, sensors, alerting and run history are worth far more
than a bespoke runner.

The steps are not reimplemented. `DailyJob` owns the logic; each Dagster op calls one of
its methods. Two implementations of "calculate the index" that could disagree is exactly
the failure this avoids.

Run the UI::

    uv sync --extra orchestration
    uv run dagster dev -m miniftse.production.dagster_defs

Materialise one day headlessly::

    uv run python -m miniftse.production.dagster_defs 2026-06-04
"""

# No `from __future__ import annotations` here, deliberately.
#
# Dagster inspects the `context` parameter's annotation at decoration time to decide
# what to inject. Postponed evaluation turns it into the string "AssetExecutionContext"
# and the check fails with a message that does not mention the future import at all.
# Every annotation in this module is 3.12-native syntax, so nothing is lost.

import datetime as dt
from typing import Any

from miniftse.config import global_all_cap
from miniftse.data.synthetic import SyntheticConfig
from miniftse.production.daily import DailyJob

try:  # pragma: no cover - exercised by the orchestration extra
    from dagster import (
        AssetExecutionContext,
        Backoff,
        DagsterEventType,
        Definitions,
        Jitter,
        MetadataValue,
        Output,
        RetryPolicy,
        ScheduleDefinition,
        asset,
        define_asset_job,
    )

    DAGSTER_AVAILABLE = True
except ImportError:  # pragma: no cover
    DAGSTER_AVAILABLE = False


class DagsterNotInstalledError(RuntimeError):
    """Raised when the orchestration extra is not installed."""


def _require_dagster() -> None:
    if not DAGSTER_AVAILABLE:
        raise DagsterNotInstalledError(
            "Dagster is not installed. `uv sync --extra orchestration`. The hand-rolled "
            "runner in production.pipeline works without it."
        )


def build_job(securities: int = 200, seed: int = 20260809,
              simulate: str | None = None) -> DailyJob:
    return DailyJob(
        config=global_all_cap(),
        universe_config=SyntheticConfig(n_securities=securities, seed=seed),
        simulate=simulate,
    )


if DAGSTER_AVAILABLE:  # pragma: no cover - requires the orchestration extra

    #: Retry only the steps whose failures are genuinely transient.
    #:
    #: Late market data is retryable: the file arrives, or it does not, and waiting is
    #: the correct response. A failed validation check is NOT retryable - the data will
    #: be just as wrong in ninety seconds, and retrying it only delays the alert and
    #: burns the window before the publication deadline.
    RETRY_TRANSIENT = RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL,
                                  jitter=Jitter.PLUS_MINUS)

    def _run_date(context: AssetExecutionContext) -> dt.date:
        """The partition key, or today. Partitioning by date is what makes a backfill
        a first-class operation rather than a script someone writes under pressure."""
        try:
            return dt.date.fromisoformat(context.partition_key)
        except Exception:
            configured = context.run.tags.get("run_date")
            return dt.date.fromisoformat(configured) if configured else dt.date.today()

    @asset(group_name="daily_production", retry_policy=RETRY_TRANSIENT,
           description="Prices, corporate actions, FX and share counts for the session.")
    def market_data(context: AssetExecutionContext) -> Output[dict[str, Any]]:
        job = build_job(simulate=context.run.tags.get("simulate"))
        run_date = _run_date(context)
        data = job.load_market_data({"run_date": run_date})
        return Output(
            {"data": data, "run_date": run_date.isoformat()},
            metadata={
                "run_date": str(run_date),
                "price_rows": len(data["prices"]),
                "corporate_actions": len(data["corp_actions"]),
                "prior_session": str(data["prior_date"]),
            },
        )

    @asset(group_name="daily_production",
           description="Schema, range and cross-source checks on the inputs.")
    def validated_inputs(context: AssetExecutionContext,
                         market_data: dict[str, Any]) -> Output[dict[str, Any]]:
        job = build_job(simulate=context.run.tags.get("simulate"))
        run_date = dt.date.fromisoformat(market_data["run_date"])
        result = job.validate_inputs(
            {"run_date": run_date, "load_market_data": market_data["data"]})
        report = result["report"]
        return Output(
            {"prices": result["prices"], "run_date": market_data["run_date"]},
            metadata={
                "checks_run": len(report.findings),
                "warnings": report.counts()["warnings"],
                "blocking": len(report.blocking),
                "report": MetadataValue.md(report.to_frame().to_markdown(index=False)),
            },
        )

    @asset(group_name="daily_production",
           description="Roll the index forward one session from the persisted state.")
    def index_level(context: AssetExecutionContext, market_data: dict[str, Any],
                    validated_inputs: dict[str, Any]) -> Output[dict[str, Any]]:
        job = build_job(simulate=context.run.tags.get("simulate"))
        run_date = dt.date.fromisoformat(market_data["run_date"])
        calc = job.calculate_index({
            "run_date": run_date, "load_market_data": market_data["data"],
            "validate_inputs": validated_inputs,
        })
        return Output(
            {"calc": calc, "run_date": market_data["run_date"]},
            metadata={
                "price_return": float(calc["price_return"]),
                "gross_total_return": float(calc["gross_total_return"]),
                "net_total_return": float(calc["net_total_return"]),
                "divisor": float(calc["state"].divisor),
                "constituents": calc["state"].n_constituents,
                "divisor_events": len(calc["audit"]),
            },
        )

    @asset(group_name="daily_production",
           description="Output validation and the publication gate. Blocks on failure.")
    def gate_decision(context: AssetExecutionContext, market_data: dict[str, Any],
                      validated_inputs: dict[str, Any],
                      index_level: dict[str, Any]) -> Output[str]:
        job = build_job(simulate=context.run.tags.get("simulate"))
        run_date = dt.date.fromisoformat(market_data["run_date"])
        ctx = {
            "run_date": run_date, "load_market_data": market_data["data"],
            "validate_inputs": validated_inputs, "calculate_index": index_level["calc"],
        }
        report = job.validate_output(ctx)
        ctx["validate_output"] = report
        # Raises on a blocking finding, which fails the asset and stops publication.
        # The gate is a node in the graph rather than a branch inside publish, so it is
        # visible in the UI and a human can see exactly what stopped.
        decision = job.publication_gate(ctx)
        return Output(decision, metadata={
            "decision": decision,
            "escalating": len(report.escalating),
            "report": MetadataValue.md(report.to_frame().to_markdown(index=False)),
        })

    @asset(group_name="daily_production",
           description="Persist the new state and write a run manifest.")
    def published_index(context: AssetExecutionContext, market_data: dict[str, Any],
                        validated_inputs: dict[str, Any], index_level: dict[str, Any],
                        gate_decision: str) -> Output[dict[str, Any]]:
        job = build_job(simulate=context.run.tags.get("simulate"))
        run_date = dt.date.fromisoformat(market_data["run_date"])
        ctx = {
            "run_date": run_date, "load_market_data": market_data["data"],
            "validate_inputs": validated_inputs, "calculate_index": index_level["calc"],
            "validate_output": None,
        }
        published = job.publish(ctx)
        ctx["publish"] = published
        context.log.info(job.notify(ctx))
        return Output(published, metadata={
            "run_id": published["run_id"],
            "state_path": published["state_path"],
            "price_return": published["levels"]["pr"],
            "gate": gate_decision,
        })

    daily_index_job = define_asset_job(
        name="daily_index_production",
        selection=["market_data", "validated_inputs", "index_level", "gate_decision",
                   "published_index"],
        description="End-to-end daily index production with a blocking quality gate.",
    )

    daily_schedule = ScheduleDefinition(
        job=daily_index_job,
        # 06:00 on weekdays. The window between this and the publication deadline is
        # what the retry policy and the runbook are sized against.
        cron_schedule="0 6 * * 1-5",
        name="daily_index_schedule",
        execution_timezone="Europe/London",
    )

    defs = Definitions(
        assets=[market_data, validated_inputs, index_level, gate_decision,
                published_index],
        jobs=[daily_index_job],
        schedules=[daily_schedule],
    )


def materialise(run_date: dt.date, simulate: str | None = None) -> dict[str, Any]:
    """Execute the Dagster job headlessly for one date.

    Used by the test suite so the orchestration is genuinely exercised rather than
    merely importable.
    """
    _require_dagster()
    from dagster import materialize  # noqa: PLC0415

    result = materialize(
        [market_data, validated_inputs, index_level, gate_decision, published_index],
        tags={"run_date": run_date.isoformat(), **({"simulate": simulate}
                                                   if simulate else {})},
        raise_on_error=False,
    )
    materialised = [
        e.node_name for e in result.events_for_node("published_index")
        if e.event_type == DagsterEventType.ASSET_MATERIALIZATION
    ] if result.success else []
    return {
        "success": result.success,
        "run_date": run_date.isoformat(),
        "materialised": materialised,
    }


if __name__ == "__main__":  # pragma: no cover
    import sys

    target = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    outcome = materialise(target, simulate=sys.argv[2] if len(sys.argv) > 2 else None)
    print(outcome)
