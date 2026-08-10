"""Command line interface.

    miniftse build-index          build a full history and run the publication gate
    miniftse chaos-drill          inject faults and report validation coverage
    miniftse pin-golden           pin the current history as the golden master
    miniftse check-golden         verify the current build against the pinned master
    miniftse daily                run the production DAG for one date
    miniftse factsheet            generate a client-facing factsheet
    miniftse sql-cookbook         emit the SQL patterns document
    miniftse runs                 list run manifests
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from miniftse.config import developed_only, global_all_cap, global_large_mid
from miniftse.data.synthetic import SyntheticConfig
from miniftse.production.build import BuildSpec, build_and_report, build_index

app = typer.Typer(add_completion=False, help="miniFTSE index platform")
console = Console()

CONFIGS = {
    "all-cap": global_all_cap,
    "large-mid": global_large_mid,
    "developed": developed_only,
}

ARTEFACTS = Path("artefacts")


def _spec(index: str, securities: int, start: str, end: str, seed: int) -> BuildSpec:
    if index not in CONFIGS:
        raise typer.BadParameter(f"unknown index {index!r}; choose from {list(CONFIGS)}")
    return BuildSpec(
        index_config=CONFIGS[index](),
        universe_config=SyntheticConfig(n_securities=securities, seed=seed),
        start=dt.date.fromisoformat(start),
        end=dt.date.fromisoformat(end),
        manifest_dir=ARTEFACTS / "manifests",
    )


@app.command("build-index")
def build_index_cmd(
    index: str = typer.Option("all-cap", help="all-cap | large-mid | developed"),
    securities: int = typer.Option(500, help="universe size"),
    start: str = typer.Option("2016-01-04"),
    end: str = typer.Option("2026-06-30"),
    seed: int = typer.Option(20260809),
    out: Path = typer.Option(ARTEFACTS, help="output directory"),
    strict: bool = typer.Option(
        False, help="exit non-zero if the publication gate blocks"),
) -> None:
    """Build a full index history and run it through the publication gate.

    A blocked gate is a legitimate outcome, not a build failure: the history is still
    computed and written, it simply must not be published without a human looking at
    it. So the command exits 0 by default and reports the block. Use `--strict` in a
    production scheduler, where a block genuinely should fail the job.
    """
    result = build_and_report(_spec(index, securities, start, end, seed))

    out.mkdir(parents=True, exist_ok=True)
    levels = out / f"{result.manifest.index_id}_levels.parquet"
    weights = out / f"{result.manifest.index_id}_weights.parquet"
    frame = result.history.levels.copy()
    frame["date"] = frame["date"].astype("datetime64[ns]")
    frame.to_parquet(levels, index=False)
    if not result.history.weights.empty:
        w = result.history.weights.copy()
        w["date"] = w["date"].astype("datetime64[ns]")
        w.to_parquet(weights, index=False)

    console.print(f"\n[green]wrote[/green] {levels}")
    if not result.history.weights.empty:
        console.print(f"[green]wrote[/green] {weights}")
    if not result.may_publish:
        console.print(
            f"\n[yellow]publication gate: {result.gate_message}[/yellow]\n"
            "The history was still computed and written. Review the findings above "
            "before distributing."
        )
        if strict:
            raise typer.Exit(code=1)


@app.command("chaos-drill")
def chaos_drill_cmd(
    securities: int = typer.Option(200),
    seed: int = typer.Option(20260809),
) -> None:
    """Inject realistic data faults and report which the validation suite catches."""
    from miniftse.quality.faults import build_baseline_context, drill_summary, run_chaos_drill

    result = build_index(
        _spec("all-cap", securities, "2016-01-04", "2020-12-31", seed), verbose=False
    )
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

    context = build_baseline_context(
        prices=today, prior_prices=yesterday, weights=weights,
        shares=universe.get_shares(None, as_of),
        fx=universe.get_fx("USD", list(universe._fx["quote"].unique()), as_of, as_of),
        prior_fx=universe.get_fx("USD", list(universe._fx["quote"].unique()),
                                 prior_dates[-1], prior_dates[-1]),
        as_of=as_of, divisor=float(last["divisor"]),
        index_level=float(last["price_return"]),
        total_market_value=float(last["total_market_value"]),
        divisor_audit=result.calculator.engine.audit_frame(),
        corp_actions=universe.get_corp_actions(None, as_of, as_of),
        config=result.manifest.config and global_all_cap(),
        prior_index_level=float(prior["price_return"]),
        prior_divisor=float(prior["divisor"]),
    )

    frame, gaps = run_chaos_drill(context)

    table = Table(title="Chaos drill", show_lines=False)
    for column in ("id", "fault", "detected", "by", "severity", "blocked"):
        table.add_column(column)
    for row in frame.itertuples(index=False):
        table.add_row(
            row.fault_id, row.fault_name,
            "[green]yes[/green]" if row.detected else "[red]NO[/red]",
            row.detected_by[:48], row.highest_severity,
            "yes" if row.blocked_publication else "no",
        )
    console.print(table)
    console.print(f"\n{drill_summary(frame)}")

    if gaps:
        console.print("\n[yellow]Coverage gaps - these are the checks to write next:"
                      "[/yellow]")
        for gap in gaps:
            console.print(f"  - {gap}")
    else:
        console.print("\n[green]No coverage gaps.[/green]")


@app.command("pin-golden")
def pin_golden_cmd(
    name: str = typer.Option("reference"),
    securities: int = typer.Option(300),
    start: str = typer.Option("2016-01-04"),
    end: str = typer.Option("2024-12-31"),
    seed: int = typer.Option(20260809),
    directory: Path = typer.Option(Path("tests/golden")),
) -> None:
    """Pin the current index history as the golden master."""
    from miniftse.production.golden import GoldenMaster

    spec = _spec("all-cap", securities, start, end, seed)
    result = build_index(spec, verbose=True)
    master = GoldenMaster.create(name, result.history.levels, spec.index_config)
    data_path, meta_path = master.save(directory)
    console.print(f"\n[green]pinned[/green] {len(master.levels)} rows")
    console.print(f"  {data_path}\n  {meta_path}")
    console.print(f"  hash {master.content_hash}")
    console.print(f"  final GTR {master.metrics.get('final_gross_total_return')}")


@app.command("check-golden")
def check_golden_cmd(
    name: str = typer.Option("reference"),
    securities: int = typer.Option(300),
    start: str = typer.Option("2016-01-04"),
    end: str = typer.Option("2024-12-31"),
    seed: int = typer.Option(20260809),
    directory: Path = typer.Option(Path("tests/golden")),
) -> None:
    """Rebuild and compare against the pinned golden master."""
    from miniftse.production.golden import GoldenMaster, compare

    master = GoldenMaster.load(directory, name)
    result = build_index(_spec("all-cap", securities, start, end, seed), verbose=False)
    comparison = compare(master, result.history.levels)
    console.print(comparison.report())
    if not comparison.passed:
        raise typer.Exit(code=1)


@app.command("daily")
def daily_cmd(
    date: str = typer.Option(None, help="run date, default the last available"),
    securities: int = typer.Option(200),
    simulate: str = typer.Option(None, help="failure mode: late_data | outlier"),
) -> None:
    """Run the production DAG for one date."""
    from miniftse.production.pipeline import (
        DataNotReadyError,
        StepStatus,
        build_daily_pipeline,
    )

    state: dict[str, int] = {"attempts": 0}

    def load(ctx: dict) -> str:
        state["attempts"] += 1
        if simulate == "late_data" and state["attempts"] <= 2:
            raise DataNotReadyError(
                f"market data has not arrived (attempt {state['attempts']})")
        return "market data loaded"

    def passthrough(label: str):
        def inner(ctx: dict) -> str:
            return label
        return inner

    def gate(ctx: dict) -> str:
        if simulate == "outlier":
            from miniftse.production.pipeline import PipelineError

            raise PipelineError(
                "publication gate BLOCKED: price_outliers - one constituent moved "
                "beyond 8 robust sigma with 34bp of index impact"
            )
        return "gate passed"

    pipeline = build_daily_pipeline(
        load_market_data=load,
        validate_inputs=passthrough("inputs valid"),
        calculate_index=passthrough("index calculated"),
        validate_output=passthrough("output valid"),
        publication_gate=gate,
        publish=passthrough("published"),
        notify=passthrough("notified"),
    )
    console.print(pipeline.describe())
    console.print()
    run = pipeline.run(dt.date.fromisoformat(date) if date else dt.date.today())
    console.print(run.summary())
    del securities
    if any(r.status == StepStatus.FAILED for r in run.results.values()):
        raise typer.Exit(code=1)


@app.command("factsheet")
def factsheet_cmd(
    securities: int = typer.Option(300),
    start: str = typer.Option("2016-01-04"),
    end: str = typer.Option("2026-06-30"),
    out: Path = typer.Option(ARTEFACTS / "factsheet.md"),
) -> None:
    """Generate a client-facing factsheet."""
    from miniftse.reporting.factsheet import write_factsheet

    result = build_index(_spec("all-cap", securities, start, end, 20260809))
    path = write_factsheet(result, out)
    console.print(f"\n[green]wrote[/green] {path}")


@app.command("sql-cookbook")
def sql_cookbook_cmd(out: Path = typer.Option(Path("docs/SQL_PATTERNS.md"))) -> None:
    """Emit the SQL patterns cookbook from the tested queries."""
    from miniftse.data.store import write_sql_cookbook

    out.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]wrote[/green] {write_sql_cookbook(out)}")


@app.command("runs")
def runs_cmd(directory: Path = typer.Option(ARTEFACTS / "manifests")) -> None:
    """List recorded run manifests."""
    from miniftse.production.manifest import ManifestStore

    frame = ManifestStore(directory).list_runs()
    if frame.empty:
        console.print("no manifests recorded")
        return
    table = Table(title="Run manifests")
    for column in frame.columns:
        table.add_column(str(column))
    for row in frame.itertuples(index=False):
        table.add_row(*[str(v) for v in row])
    console.print(table)


@app.command("methodology")
def methodology_cmd(out: Path = typer.Option(Path("ground_rules/factor_methodology.md"))
                    ) -> None:
    """Generate the factor methodology document from the factor definitions."""
    from miniftse.factors.definitions import ALL_FACTORS

    out.parent.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(f.describe() for f in ALL_FACTORS.values())
    out.write_text(
        "# Factor definitions\n\n"
        "Generated from `miniftse.factors.definitions`. Editing this file by hand will "
        "not change the code, and the code is what computes the index.\n\n" + body,
        encoding="utf-8",
    )
    console.print(f"[green]wrote[/green] {out}")


@app.command("summary")
def summary_cmd(securities: int = typer.Option(300)) -> None:
    """Print the reference universe summary."""
    from miniftse.data.synthetic import SyntheticUniverse

    universe = SyntheticUniverse(SyntheticConfig(n_securities=securities))
    console.print_json(json.dumps(universe.summary(), default=str))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
