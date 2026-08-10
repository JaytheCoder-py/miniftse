"""Command line interface.

    miniftse build-index          build a full history and run the publication gate
    miniftse chaos-drill          inject faults and report validation coverage
    miniftse desk-snapshot        precompute every artefact the ops desk serves
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
    from miniftse.quality.faults import baseline_from_build, drill_summary, run_chaos_drill

    result = build_index(
        _spec("all-cap", securities, "2016-01-04", "2020-12-31", seed), verbose=False
    )
    frame, gaps = run_chaos_drill(baseline_from_build(result))

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


@app.command("desk-snapshot")
def desk_snapshot_cmd(
    out: Path = typer.Option(Path("desk/data"), help="snapshot output directory"),
    securities: int = typer.Option(300),
    start: str = typer.Option("2016-01-04"),
    end: str = typer.Option("2024-12-31"),
    seed: int = typer.Option(20260809),
) -> None:
    """Build every artefact the ops desk serves. Run before deploying.

    One reference build produces the lot, so the deployed application never recomputes
    anything: it loads these files at startup and serves from memory. The command
    either writes a complete snapshot or fails - a half-written one must never be
    committed.
    """
    from miniftse.desk.snapshot import EXPECTED_FILES, build_snapshot, reference_spec

    spec = reference_spec(
        securities=securities,
        start=dt.date.fromisoformat(start),
        end=dt.date.fromisoformat(end),
        seed=seed,
    )
    manifest = build_snapshot(out, spec)

    console.print(f"\n[green]wrote[/green] {len(EXPECTED_FILES)} artefacts to {out}")
    console.print(f"  git sha {manifest.git_sha[:12]}"
                  f"{' [yellow](dirty tree)[/yellow]' if manifest.git_dirty else ''}")
    console.print(f"  built in {manifest.duration_seconds}s")


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


@app.command("seed-state")
def seed_state_cmd(
    securities: int = typer.Option(300),
    up_to: str = typer.Option("2026-06-01"),
    seed: int = typer.Option(20260809),
) -> None:
    """Build the history once and persist the closing state the daily job resumes from."""
    from miniftse.production.daily import DailyJob

    job = DailyJob(config=global_all_cap(),
                   universe_config=SyntheticConfig(n_securities=securities, seed=seed))
    path = job.seed_state(dt.date.fromisoformat(up_to))
    console.print(f"[green]seeded[/green] {path}")


@app.command("daily")
def daily_cmd(
    date: str = typer.Option(None, help="run date; default the next session after state"),
    securities: int = typer.Option(300),
    seed: int = typer.Option(20260809),
    simulate: str = typer.Option(
        None, help="failure mode: late_data | outlier | missing_corp_action"),
    show_dag: bool = typer.Option(True),
) -> None:
    """Run the real daily production DAG for one date.

    This performs the genuine calculation: it loads the day's data, validates it, rolls
    the index forward one day from the persisted state, validates the output, passes the
    publication gate and writes the new state and a run manifest.
    """
    from miniftse.production.daily import DailyJob, IndexStateFile
    from miniftse.production.pipeline import StepStatus

    job = DailyJob(config=global_all_cap(),
                   universe_config=SyntheticConfig(n_securities=securities, seed=seed),
                   simulate=simulate)

    stored = IndexStateFile.load(job.state_dir, job.config.index_id)
    if stored is None:
        console.print("[yellow]no stored state; seeding it first "
                      "(this is also the disaster-recovery path)[/yellow]")
        job.seed_state(dt.date(2026, 6, 1), verbose=False)
        stored = IndexStateFile.load(job.state_dir, job.config.index_id)

    if date:
        run_date = dt.date.fromisoformat(date)
    else:
        assert stored is not None
        prior = dt.date.fromisoformat(stored.as_of)
        sessions = sorted(d for d in job.universe.calendar.date if d > prior)
        if not sessions:
            console.print("[red]no session after the stored state date[/red]")
            raise typer.Exit(code=1)
        run_date = sessions[0]

    if show_dag:
        console.print(job.pipeline().describe())
        console.print()

    if stored is not None:
        console.print(
            f"resuming from {stored.as_of} (level {stored.level_pr:,.2f}), "
            f"running {run_date}"
        )
        console.print()
    run = job.run(run_date)
    console.print(run.summary())

    if run.succeeded:
        published = run.context["publish"]
        console.print()
        console.print(
            f"[green]published[/green] PR {published['levels']['pr']:,.2f} "
            f"GTR {published['levels']['gtr']:,.2f}"
        )
        console.print(run.context["notify"])
    else:
        console.print()
        console.print("[red]not published.[/red] See docs/RUNBOOK.md")
        for result in run.results.values():
            if result.status == StepStatus.FAILED:
                console.print(f"  {result.name}: {result.error}")
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


@app.command("documents")
def documents_cmd(
    out: str = typer.Option("docs", help="output directory for the long-form documents"),
) -> None:
    """Generate every document: memos, research paper, incident report, vocabulary map.

    Generated rather than hand-maintained so a document cannot quote a figure the
    repository no longer produces.
    """
    from miniftse.reporting.papers import document_index, write_all_documents

    paths = write_all_documents(Path(out))
    frame = document_index(paths)
    console.print(f"[green]wrote {len(paths)} documents[/green]")
    for row in frame.itertuples(index=False):
        console.print(f"  {row.path}  ({row.size_kb} kB)")


@app.command("reconcile")
def reconcile_cmd(
    securities: int = typer.Option(200),
    seed: int = typer.Option(20260809),
    out: str = typer.Option("artefacts/reconciliation_study.md"),
) -> None:
    """Reconcile the index against a comparison series, constituents first.

    Two indices can post matching returns for a week while holding entirely different
    securities, so the study reconciles holdings before returns and reports the
    unexplained residual rather than absorbing it.
    """
    import pandas as pd

    from miniftse.production.build import BuildSpec, build_index
    from miniftse.quality.reconciliation import (
        reconcile_against_published,
        synthetic_published_index,
        write_reconciliation_study,
    )

    spec = BuildSpec(index_config=global_all_cap(),
                     universe_config=SyntheticConfig(n_securities=securities, seed=seed))
    result = build_index(spec, verbose=True)

    weights = result.history.weights
    last = weights["date"].max()
    ours = weights[weights["date"] == last].set_index("security_id")["weight"]
    theirs = synthetic_published_index(ours)

    levels = result.history.levels.set_index("date")["gross_total_return"]
    their_levels = pd.Series(
        levels.to_numpy() * (1 - 0.0020 * (pd.RangeIndex(len(levels)) / 252.0)),
        index=levels.index,
    )

    study = reconcile_against_published(ours, theirs, levels, their_levels,
                                        as_of=last, fee_bps=20.0)
    console.print(study["constituent_verdict"])
    if "return_verdict" in study:
        console.print(study["return_verdict"])
    path = write_reconciliation_study(study, "miniFTSE Global All Cap", Path(out))
    console.print(f"[green]wrote[/green] {path}")
