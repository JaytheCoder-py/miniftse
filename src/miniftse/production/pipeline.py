"""The daily production pipeline, modelled as a DAG.

    load market data -> validate inputs -> apply corporate actions -> calculate
    -> validate output -> PUBLICATION GATE -> publish -> notify

Written as an explicit dependency graph rather than a script for three reasons that
only matter once it is running unattended at 6am:

* **A failed step names itself.** "The 6am index calculation failed" is not actionable;
  "the corporate-actions step failed because the event file had not arrived" is.
* **Retries can be per-step.** A transient network failure loading FX should retry;
  a validation failure should not - retrying a blocking check until it passes is how
  bad data gets published.
* **The gate is a node, not an if-statement.** It appears in the graph, in the run log
  and in the runbook, and it cannot be skipped by a code path someone adds later.

`Dagster` or `Airflow` would supply the scheduler. The graph, the retry policy and the
gate are the part that is specific to an index, so they live here and the orchestrator
is a thin wrapper.
"""

from __future__ import annotations

import datetime as dt
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    """Upstream failed, so this never ran. Distinct from FAILED - which one it is
    decides whether anyone needs to look at this step at all."""


class PipelineError(RuntimeError):
    pass


class DataNotReadyError(PipelineError):
    """An input has not arrived. Retryable - this is the late-data case, and waiting is
    the correct response."""


class ValidationFailedError(PipelineError):
    """A blocking check failed. **Not** retryable: the data is wrong, and running the
    same calculation again will produce the same wrong answer."""


@dataclass
class StepResult:
    name: str
    status: StepStatus
    duration: float = 0.0
    attempts: int = 1
    output: Any = None
    error: str | None = None
    traceback: str | None = None
    logs: list[str] = field(default_factory=list)


@dataclass
class Step:
    """One node. `run` receives the accumulated context and returns its output."""

    name: str
    run: Callable[[dict[str, Any]], Any]
    depends_on: tuple[str, ...] = ()
    retries: int = 0
    retry_delay: float = 0.0
    retry_on: tuple[type[Exception], ...] = (DataNotReadyError,)
    """Only these exception types are retried. Defaulting to everything is the trap:
    it turns a deterministic validation failure into a slow deterministic validation
    failure."""

    timeout: float | None = None
    critical: bool = True
    """A non-critical step that fails does not block downstream work. Notification is
    non-critical; calculation is not."""

    description: str = ""


@dataclass
class PipelineRun:
    run_date: dt.date
    results: dict[str, StepResult] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    started: dt.datetime | None = None
    finished: dt.datetime | None = None

    @property
    def succeeded(self) -> bool:
        return all(
            r.status in {StepStatus.SUCCESS, StepStatus.SKIPPED}
            for r in self.results.values()
        )

    @property
    def failed_steps(self) -> list[StepResult]:
        return [r for r in self.results.values() if r.status == StepStatus.FAILED]

    def summary(self) -> str:
        lines = [
            f"Pipeline run for {self.run_date}: "
            f"{'SUCCESS' if self.succeeded else 'FAILED'}"
        ]
        for r in self.results.values():
            mark = {
                StepStatus.SUCCESS: "ok", StepStatus.FAILED: "FAIL",
                StepStatus.BLOCKED: "blocked", StepStatus.SKIPPED: "skipped",
            }.get(r.status, "?")
            line = f"  [{mark:>7}] {r.name} ({r.duration:.2f}s"
            line += f", {r.attempts} attempts)" if r.attempts > 1 else ")"
            if r.error:
                line += f"\n            {r.error}"
            lines.append(line)
        return "\n".join(lines)

    def timings(self) -> dict[str, float]:
        return {name: r.duration for name, r in self.results.items()}


@dataclass
class Pipeline:
    """A DAG of steps with dependency-ordered execution."""

    name: str
    steps: list[Step] = field(default_factory=list)
    on_failure: Callable[[StepResult, PipelineRun], None] | None = None

    def add(self, step: Step) -> Pipeline:
        self.steps.append(step)
        return self

    def validate_graph(self) -> list[str]:
        """Missing dependencies and cycles. Checked before running, not during."""
        names = {s.name for s in self.steps}
        problems = [
            f"step '{s.name}' depends on unknown step '{d}'"
            for s in self.steps for d in s.depends_on if d not in names
        ]
        try:
            self._topological_order()
        except PipelineError as exc:
            problems.append(str(exc))
        return problems

    def _topological_order(self) -> list[Step]:
        by_name = {s.name: s for s in self.steps}
        visited: dict[str, int] = {}
        order: list[Step] = []

        def visit(name: str, path: tuple[str, ...]) -> None:
            state = visited.get(name, 0)
            if state == 1:
                raise PipelineError(
                    f"dependency cycle: {' -> '.join([*path, name])}"
                )
            if state == 2:
                return
            visited[name] = 1
            for dep in by_name[name].depends_on:
                if dep in by_name:
                    visit(dep, (*path, name))
            visited[name] = 2
            order.append(by_name[name])

        for step in self.steps:
            visit(step.name, ())
        return order

    def run(self, run_date: dt.date, context: dict[str, Any] | None = None
            ) -> PipelineRun:
        problems = self.validate_graph()
        if problems:
            raise PipelineError("invalid pipeline: " + "; ".join(problems))

        run = PipelineRun(run_date=run_date, context=dict(context or {}),
                          started=dt.datetime.now(dt.UTC))
        run.context["run_date"] = run_date

        for step in self._topological_order():
            upstream_failed = [
                d for d in step.depends_on
                if run.results.get(d) and run.results[d].status == StepStatus.FAILED
            ]
            if upstream_failed:
                run.results[step.name] = StepResult(
                    name=step.name, status=StepStatus.BLOCKED,
                    error=f"upstream step(s) failed: {', '.join(upstream_failed)}",
                )
                continue

            run.results[step.name] = self._execute(step, run)
            result = run.results[step.name]
            if result.status == StepStatus.SUCCESS:
                run.context[step.name] = result.output
            elif step.critical and self.on_failure:
                self.on_failure(result, run)

        run.finished = dt.datetime.now(dt.UTC)
        return run

    def _execute(self, step: Step, run: PipelineRun) -> StepResult:
        attempts = 0
        started = time.perf_counter()
        last: Exception | None = None

        while attempts <= step.retries:
            attempts += 1
            try:
                output = step.run(run.context)
                return StepResult(
                    name=step.name, status=StepStatus.SUCCESS,
                    duration=time.perf_counter() - started, attempts=attempts,
                    output=output,
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not isinstance(exc, step.retry_on) or attempts > step.retries:
                    break
                time.sleep(step.retry_delay)

        return StepResult(
            name=step.name, status=StepStatus.FAILED,
            duration=time.perf_counter() - started, attempts=attempts,
            error=f"{type(last).__name__}: {last}",
            traceback=traceback.format_exc(),
        )

    def describe(self) -> str:
        """The DAG as text, for the runbook."""
        lines = [f"Pipeline: {self.name}", ""]
        for step in self._topological_order():
            deps = f" <- {', '.join(step.depends_on)}" if step.depends_on else ""
            flags = []
            if step.retries:
                flags.append(f"{step.retries} retries")
            if not step.critical:
                flags.append("non-critical")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"  {step.name}{deps}{suffix}")
            if step.description:
                lines.append(f"      {step.description}")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------


def build_daily_pipeline(
    load_market_data: Callable[[dict[str, Any]], Any],
    validate_inputs: Callable[[dict[str, Any]], Any],
    calculate_index: Callable[[dict[str, Any]], Any],
    validate_output: Callable[[dict[str, Any]], Any],
    publication_gate: Callable[[dict[str, Any]], Any],
    publish: Callable[[dict[str, Any]], Any],
    notify: Callable[[dict[str, Any]], Any],
    on_failure: Callable[[StepResult, PipelineRun], None] | None = None,
) -> Pipeline:
    """The standard daily index production graph.

    Retry policy is the interesting part. Data loading retries - late files are normal
    and waiting is the right response. Validation does not - a blocking check failing
    means the data is wrong, and retrying is at best a waste and at worst how bad data
    reaches clients. Notification is non-critical: a failed Slack message must not stop
    an otherwise good index from publishing.
    """
    return (
        Pipeline(name="daily-index-production", on_failure=on_failure)
        .add(Step(
            "load_market_data", load_market_data, retries=3, retry_delay=30.0,
            retry_on=(DataNotReadyError, ConnectionError, TimeoutError),
            description="Fetch prices, FX, corporate actions and reference data. "
                        "Retries because late files are routine.",
        ))
        .add(Step(
            "validate_inputs", validate_inputs, depends_on=("load_market_data",),
            retries=0,
            description="Schema, range and cross-source checks on raw inputs. No "
                        "retry: a failure here means the data is wrong, not late.",
        ))
        .add(Step(
            "calculate_index", calculate_index,
            depends_on=("validate_inputs",), retries=0,
            description="Apply corporate actions, roll the divisor, compute PR, GTR "
                        "and NTR.",
        ))
        .add(Step(
            "validate_output", validate_output, depends_on=("calculate_index",),
            retries=0,
            description="Aggregate, temporal and reconciliation checks on the "
                        "calculated index.",
        ))
        .add(Step(
            "publication_gate", publication_gate, depends_on=("validate_output",),
            retries=0,
            description="THE GATE. Blocks publication on any blocking finding. Not "
                        "overridable in code - an override is a signed human decision.",
        ))
        .add(Step(
            "publish", publish, depends_on=("publication_gate",), retries=2,
            retry_delay=10.0, retry_on=(ConnectionError, TimeoutError),
            description="Write levels and constituents to the distribution store.",
        ))
        .add(Step(
            "notify", notify, depends_on=("publish",), retries=1, critical=False,
            description="Tell downstream consumers. Non-critical: a failed "
                        "notification must not block a good index.",
        ))
    )


@dataclass
class FailureSimulator:
    """Inject pipeline failures to prove the DAG handles them.

    The three modes worth drilling, because they are the three that actually happen and
    they need three different responses:

    * **late data** - retryable, the pipeline should wait and succeed
    * **a price outlier** - the gate should block publication
    * **a missing corporate action** - the gate should block and escalate
    """

    mode: str
    fire_on_attempt: int = 1
    _attempts: int = 0

    def wrap(self, step_fn: Callable[[dict[str, Any]], Any]
             ) -> Callable[[dict[str, Any]], Any]:
        def wrapped(context: dict[str, Any]) -> Any:
            self._attempts += 1
            if self.mode == "late_data" and self._attempts <= self.fire_on_attempt:
                raise DataNotReadyError(
                    f"market data file has not arrived (attempt {self._attempts})"
                )
            if self.mode == "permanent_failure":
                raise PipelineError("permanent upstream failure")
            return step_fn(context)

        return wrapped
