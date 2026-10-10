"""Command-line interface: `evals snapshot | validate | run | report | compare`."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from agent_evals.config import Config, load_config

if TYPE_CHECKING:
    from agent_evals.store.db import ResultsStore

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="YAML config file.")]
DEFAULT_CONFIG = Path("configs/default.yaml")


def _config(path: Path) -> Config:
    return load_config(path if path.exists() else None)


@app.command()
def snapshot(
    pages: Annotated[Path, typer.Option(help="File with one URL per line.")] = Path(
        "configs/docs_pages.txt"
    ),
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Fetch AWS documentation pages into a dated local snapshot."""
    from agent_evals.agents.docs.snapshot import build_snapshot, read_url_list

    cfg = _config(config)
    urls = read_url_list(pages)
    console.print(f"Fetching {len(urls)} pages into {cfg.paths.corpus_dir} ...")
    report = build_snapshot(urls, cfg.paths.corpus_dir)
    console.print(f"[green]Saved {report.pages} pages.[/green]")
    for f in report.failures:
        console.print(f"[yellow]Failed[/yellow] {f['url']}: {f['error']}")
    if report.failures:
        raise typer.Exit(code=1)


@app.command()
def validate(suite: Path) -> None:
    """Check a suite file for schema errors, duplicate ids and missing gold answers."""
    from agent_evals.suites import SuiteError, load_suite

    try:
        cases = load_suite(suite)
    except SuiteError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    by_type: dict[str, int] = {}
    for c in cases:
        by_type[c.type] = by_type.get(c.type, 0) + 1
    console.print(f"[green]{len(cases)} valid cases[/green] " + str(by_type))


@app.command()
def run(
    suite: Path,
    agent: Annotated[str, typer.Option(help="Agent to evaluate.")] = "docs",
    attempts: Annotated[int, typer.Option(min=1, help="Attempts per case (for pass^k).")] = 1,
    limit: Annotated[int | None, typer.Option(help="Only run the first N cases.")] = None,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Run an agent on a suite, grade every attempt, and store the results."""
    load_dotenv()
    from agent_evals.agents.docs.agent import DocsAgent
    from agent_evals.agents.docs.corpus import Corpus
    from agent_evals.graders.deterministic import (
        AnswerGrader,
        CitationGrader,
        FormatGrader,
        TrajectoryGrader,
    )
    from agent_evals.harness.cache import CallCache
    from agent_evals.harness.llm import AnthropicClient
    from agent_evals.harness.manifest import build_manifest
    from agent_evals.harness.runner import run_suite
    from agent_evals.store.db import ResultsStore
    from agent_evals.suites import load_suite
    from agent_evals.types import Grader

    if agent != "docs":
        console.print(f"[red]Unknown agent {agent!r}. Available: docs[/red]")
        raise typer.Exit(code=2)

    cfg = _config(config)
    cases = load_suite(suite)[:limit]
    corpus = Corpus.load(cfg.paths.corpus_dir)
    llm = AnthropicClient(CallCache(cfg.paths.cache_dir))
    docs_agent = DocsAgent(llm, corpus, cfg.docs_agent)
    graders: list[Grader] = [
        AnswerGrader(),
        CitationGrader(corpus),
        FormatGrader(),
        TrajectoryGrader(),
    ]
    manifest = build_manifest(
        agent=docs_agent,
        suite_path=suite,
        corpus_fingerprint=corpus.fingerprint,
        config=cfg.model_dump(mode="json"),
        attempts=attempts,
    )
    store = ResultsStore(cfg.paths.db)
    try:
        run_id = run_suite(
            agent=docs_agent,
            cases=cases,
            graders=graders,
            store=store,
            suite_name=suite.stem,
            attempts=attempts,
            workers=cfg.run.workers,
            pricing=cfg.docs_agent.model.pricing,
            budget_usd=cfg.run.budget_usd,
            manifest=manifest,
        )
        _print_summary(store, run_id)
    finally:
        store.close()


@app.command()
def report(
    run_id: Annotated[str | None, typer.Argument(help="Defaults to the latest run.")] = None,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Summarize a run: every metric with a 95% confidence interval."""
    from agent_evals.store.db import ResultsStore

    store = ResultsStore(_config(config).paths.db)
    try:
        rid = run_id or store.latest_run_id()
        if rid is None:
            console.print("No runs yet. Try `evals run suites/docs/smoke.jsonl`.")
            raise typer.Exit(code=1)
        _print_summary(store, rid)
    finally:
        store.close()


@app.command("compare")
def compare_cmd(
    baseline: str,
    candidate: str,
    metric: Annotated[str, typer.Option(help="Metric to compare.")] = "correct",
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Paired comparison of two runs on their shared cases."""
    from agent_evals.report import compare
    from agent_evals.store.db import ResultsStore

    store = ResultsStore(_config(config).paths.db)
    try:
        c = compare(store, baseline, candidate, metric)
    finally:
        store.close()
    d = c.diff
    console.print(f"[bold]{metric}[/bold] on {c.n_cases} shared cases")
    console.print(f"  baseline  {c.baseline:.3f}\n  candidate {c.candidate:.3f}")
    console.print(f"  difference {d.mean:+.3f}  (95% CI {d.low:+.3f} to {d.high:+.3f})")
    console.print(f"  McNemar p = {c.p_value:.3g}")
    verdict = "improved" if d.low > 0 else "regressed" if d.high < 0 else "no detectable change"
    console.print(f"  verdict: [bold]{verdict}[/bold]")
    if c.only_baseline_passed:
        console.print(f"  newly failing: {', '.join(c.only_baseline_passed)}")
    if c.only_candidate_passed:
        console.print(f"  newly passing: {', '.join(c.only_candidate_passed)}")


def _print_summary(store: ResultsStore, run_id: str) -> None:
    from agent_evals.report import LOWER_IS_BETTER, NEUTRAL, summarize

    s = summarize(store, run_id)
    info = s.info
    console.print(
        f"\n[bold]{run_id}[/bold]  agent={info['agent']}@{info['agent_version']}  "
        f"suite={info['suite']}  status={info['status']}"
    )
    table = Table(show_header=True, header_style="bold")
    for col in ("metric", "mean", "95% CI", "cases", "better"):
        table.add_column(col, justify="right" if col != "metric" else "left")
    for name, est in s.metrics.items():
        table.add_row(
            name,
            f"{est.mean:.3f}",
            f"{est.low:.3f} to {est.high:.3f}",
            str(est.n),
            "-" if name in NEUTRAL else "lower" if name in LOWER_IS_BETTER else "higher",
        )
    console.print(table)
    if s.pass_hat_k:
        console.print("pass^k: " + "  ".join(f"k={k}: {v:.3f}" for k, v in s.pass_hat_k.items()))
    t = s.totals
    console.print(
        f"{t['attempts']} attempts, {t['model_calls']} model calls "
        f"({t['cached_calls']} from cache), {t['input_tokens']} in / {t['output_tokens']} out "
        f"tokens, {t['errors']} errors"
        + (f", nominal cost ${t['cost_usd']:.4f}" if t["cost_usd"] is not None else "")
    )
    if s.failures:
        console.print("\n[bold]Failures[/bold]")
        for case_id, attempt, reason in s.failures:
            console.print(f"  {case_id} #{attempt}: {reason}")


if __name__ == "__main__":
    app()
