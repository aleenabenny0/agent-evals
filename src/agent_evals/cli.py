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
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Fetch and compare with the lock file instead of writing; exit 1 on drift.",
        ),
    ] = False,
    report: Annotated[
        Path | None, typer.Option(help="With --check: also write the markdown report here.")
    ] = None,
    relock: Annotated[
        bool,
        typer.Option("--relock", help="Write the lock file from the local snapshot; no fetch."),
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Fetch AWS documentation pages into a dated local snapshot, or check it for drift."""
    from agent_evals.agents.docs.snapshot import (
        SnapshotLock,
        build_snapshot,
        diff_snapshot,
        drift_report,
        fetch_pages,
        read_url_list,
    )

    cfg = _config(config)
    lock_path = cfg.paths.docs_lock
    if check and relock:
        console.print("[red]--check and --relock are exclusive.[/red]")
        raise typer.Exit(code=2)

    if relock:
        from agent_evals.corpus import Corpus

        corpus = Corpus.load(cfg.paths.corpus_dir)
        created_at = next(iter(corpus.pages.values())).fetched_at
        SnapshotLock.from_pages(corpus.pages.values(), created_at).save(lock_path)
        console.print(f"[green]Wrote {lock_path} for {len(corpus.pages)} pages.[/green]")
        return

    urls = read_url_list(pages)
    if check:
        from agent_evals.suites import evidence_index

        if not lock_path.exists():
            console.print(f"[red]No lock file at {lock_path}. Run `evals snapshot` first.[/red]")
            raise typer.Exit(code=2)
        lock = SnapshotLock.load(lock_path)
        console.print(f"Checking {len(urls)} pages against {lock_path} ...")
        fetched, failures, checked_at = fetch_pages(urls)
        diff = diff_snapshot(fetched, failures, lock, checked_at=checked_at)
        cited = evidence_index(sorted(cfg.paths.suites_dir.rglob("*.jsonl")))
        affected = {u: cited[u] for u in diff.changed + diff.missing if u in cited}
        text = drift_report(diff, lock, affected)
        if report is not None:
            report.write_text(text, encoding="utf-8")
        console.print(text, markup=False, highlight=False)
        raise typer.Exit(code=1 if diff.drifted else 0)

    console.print(f"Fetching {len(urls)} pages into {cfg.paths.corpus_dir} ...")
    result = build_snapshot(urls, cfg.paths.corpus_dir, lock_path=lock_path)
    console.print(f"[green]Saved {result.pages} pages; lock written to {lock_path}.[/green]")
    for f in result.failures:
        console.print(f"[yellow]Failed[/yellow] {f['url']}: {f['error']}")
    if result.failures:
        raise typer.Exit(code=1)


@app.command()
def validate(suite: Path, config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Check a suite: schema, duplicate ids, gold answers; with a snapshot, evidence and quotes."""
    from agent_evals.corpus import Corpus
    from agent_evals.suites import (
        SuiteError,
        check_against_corpus,
        load_suite,
        unassigned,
        unverified_drafts,
    )

    try:
        cases = load_suite(suite)
    except SuiteError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    by_type: dict[str, int] = {}
    for c in cases:
        by_type[c.type] = by_type.get(c.type, 0) + 1
    console.print(f"[green]{len(cases)} valid cases[/green] " + str(by_type))
    if drafts := unverified_drafts(cases):
        console.print(f"{len(drafts)} unverified drafts; runs refuse them without --allow-drafts")
    if open_split := unassigned(cases):
        console.print(f"{len(open_split)} cases with split 'unassigned'; use `evals split`")
    if held := [c.id for c in cases if c.hold]:
        console.print(f"{len(held)} on hold (never placed in test): {', '.join(held)}")

    corpus_dir = _config(config).paths.corpus_dir
    if not (corpus_dir / "pages.jsonl").exists():
        console.print(
            "[yellow]No snapshot here, so evidence and quote checks were skipped.[/yellow]"
        )
        return
    errors, warnings = check_against_corpus(cases, Corpus.load(corpus_dir))
    for w in warnings:
        console.print(f"[yellow]warning[/yellow] {w}")
    for e in errors:
        console.print(f"[red]error[/red] {e}")
    if errors:
        raise typer.Exit(code=1)


@app.command()
def review(
    suite: Path,
    reviewer: Annotated[str, typer.Option(help="Your name; written to verified_by.")],
    limit: Annotated[int | None, typer.Option(help="Review at most N drafts this time.")] = None,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Walk through unverified drafts one by one, with the support quote shown in its page."""
    import sys

    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.text import Text

    from agent_evals.corpus import Corpus
    from agent_evals.review import accept, quote_context, reject
    from agent_evals.suites import load_suite, write_suite

    if not sys.stdin.isatty():
        console.print("[red]evals review needs an interactive terminal.[/red]")
        raise typer.Exit(code=2)
    cfg = _config(config)
    corpus = None
    if (cfg.paths.corpus_dir / "pages.jsonl").exists():
        corpus = Corpus.load(cfg.paths.corpus_dir)
    else:
        console.print("[yellow]No snapshot: quotes are shown without page context.[/yellow]")

    cases = load_suite(suite)
    drafts = [c for c in cases if c.is_draft][:limit]
    if not drafts:
        console.print("Nothing to review: no unverified drafts in this file.")
        return
    base = suite.name.removesuffix(".jsonl").removesuffix(".drafts")
    rejected_path = suite.with_name(f"{base}.rejected.jsonl")
    rejected = load_suite(rejected_path) if rejected_path.exists() else []
    tally = {"accepted": 0, "rejected": 0, "skipped": 0}

    for i, c in enumerate(drafts, 1):
        console.rule(f"{c.id}   {c.type}, {c.area}   {i}/{len(drafts)}")
        console.print(Panel(c.question, title="question"))
        console.print(Panel(c.gold.answer, title="gold answer"))
        console.print(f"must_include: {c.gold.must_include}")
        for url in c.gold.evidence_urls:
            console.print(f"evidence: {url}")
        if c.gold.support:
            shown = False
            if corpus is not None:
                for url in c.gold.evidence_urls:
                    page = corpus.pages.get(url)
                    ctx = quote_context(page.text, c.gold.support) if page else None
                    if ctx:
                        before, match, after = ctx
                        body = Text.assemble(before, (match, "bold yellow"), after)
                        console.print(Panel(body, title=f"support, in context of {url}"))
                        shown = True
                        break
            if not shown:
                console.print(Panel(c.gold.support, title="support quote"))
        if c.notes:
            console.print(f"notes: {c.notes}")

        try:
            choice = Prompt.ask("(a)ccept, (r)eject, (s)kip, (q)uit", choices=["a", "r", "s", "q"])
        except EOFError:
            console.print("[yellow]No interactive input; stopping.[/yellow]")
            break
        if choice == "q":
            break
        if choice == "a":
            cases = [accept(x, reviewer) if x.id == c.id else x for x in cases]
            tally["accepted"] += 1
        elif choice == "r":
            try:
                reason = Prompt.ask("reason")
            except EOFError:
                reason = ""
            rejected.append(reject(c, reviewer, reason))
            cases = [x for x in cases if x.id != c.id]
            tally["rejected"] += 1
        else:
            tally["skipped"] += 1
            continue
        write_suite(suite, cases)  # saved after every decision, so quitting loses nothing
        if rejected:
            write_suite(rejected_path, rejected)

    remaining = sum(1 for x in cases if x.is_draft)
    console.print(
        f"\naccepted {tally['accepted']}, rejected {tally['rejected']}, skipped "
        f"{tally['skipped']}; {remaining} unverified draft(s) remain in {suite}"
    )
    if rejected:
        console.print(f"rejected cases are kept with reasons in {rejected_path}")


@app.command()
def split(
    source: Path,
    dev: Annotated[int, typer.Option(min=0, help="Cases for the dev split.")],
    test: Annotated[int, typer.Option(min=0, help="Cases for the held-out test split.")],
    seed: Annotated[int, typer.Option(help="Random seed. Record it in HANDOFF.md.")] = 0,
    out_dir: Annotated[Path, typer.Option(help="Writes dev.jsonl and test.jsonl here.")] = Path(
        "suites/docs"
    ),
    allow_drafts: Annotated[
        bool, typer.Option("--allow-drafts", help="Split unverified drafts too (not for real).")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files.")] = False,
) -> None:
    """Assign verified cases to dev/test at random within each (type, area) stratum."""
    from agent_evals.suites import load_suite, stratified_split, unverified_drafts, write_suite

    cases = load_suite(source)
    drafts = unverified_drafts(cases)
    if drafts and not allow_drafts:
        console.print(
            f"[red]{len(drafts)} unverified drafts (e.g. {drafts[0]!r}); verify first.[/red]"
        )
        raise typer.Exit(code=2)
    try:
        dev_cases, test_cases = stratified_split(cases, dev=dev, test=test, seed=seed)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    targets = {out_dir / "dev.jsonl": dev_cases, out_dir / "test.jsonl": test_cases}
    for path in targets:
        if path.exists() and not force:
            console.print(f"[red]{path} exists; pass --force to overwrite.[/red]")
            raise typer.Exit(code=2)
    for path, group in targets.items():
        write_suite(path, group)
        mix: dict[str, int] = {}
        for c in group:
            mix[c.type] = mix.get(c.type, 0) + 1
        console.print(f"[green]{path}[/green] {len(group)} cases {mix}")
    if held := [c.id for c in dev_cases if c.hold]:
        console.print(f"{len(held)} held case(s) placed in dev: {', '.join(held)}")
    console.print(f"seed={seed}; record it with the suite in HANDOFF.md")


@app.command()
def run(
    suite: Path,
    agent: Annotated[
        str, typer.Option(help="Agent to evaluate: docs, closed-book or oracle-evidence.")
    ] = "docs",
    attempts: Annotated[int, typer.Option(min=1, help="Attempts per case (for pass^k).")] = 1,
    limit: Annotated[int | None, typer.Option(help="Only run the first N cases.")] = None,
    allow_drafts: Annotated[
        bool,
        typer.Option(
            "--allow-drafts",
            help="Score unverified drafts and unassigned cases too. Never for a release decision.",
        ),
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Run an agent on a suite, grade every attempt, and store the results."""
    load_dotenv()
    from agent_evals.agents.docs.agent import DocsAgent
    from agent_evals.agents.docs.baselines import ClosedBookAgent, OracleEvidenceAgent
    from agent_evals.corpus import Corpus
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
    from agent_evals.suites import load_suite, unassigned, unverified_drafts
    from agent_evals.types import Grader

    agents: dict[str, type[DocsAgent]] = {
        "docs": DocsAgent,
        "closed-book": ClosedBookAgent,
        "oracle-evidence": OracleEvidenceAgent,
    }
    if agent not in agents:
        console.print(f"[red]Unknown agent {agent!r}. Available: {', '.join(agents)}[/red]")
        raise typer.Exit(code=2)

    cfg = _config(config)
    cases = load_suite(suite)[:limit]
    blocked = list(dict.fromkeys(unverified_drafts(cases) + unassigned(cases)))
    if blocked and not allow_drafts:
        console.print(
            f"[red]{len(blocked)} case(s) are unverified drafts or have no split (e.g. "
            f"{blocked[0]!r}). Verify and `evals split` them, or pass --allow-drafts for a "
            "throwaway run.[/red]"
        )
        raise typer.Exit(code=2)
    corpus = Corpus.load(cfg.paths.corpus_dir)
    llm = AnthropicClient(CallCache(cfg.paths.cache_dir))
    docs_agent = agents[agent](llm, corpus, cfg.docs_agent)
    graders: list[Grader] = [
        AnswerGrader(),
        CitationGrader(corpus, min_quote_chars=cfg.graders.min_quote_chars),
        FormatGrader(),
        TrajectoryGrader(),
    ]
    manifest = build_manifest(
        agent=docs_agent,
        graders=graders,
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
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        store.close()
    d = c.diff
    better = "lower" if c.lower_is_better else "higher"
    console.print(f"[bold]{metric}[/bold] on {c.n_cases} shared cases ({better} is better)")
    console.print(f"  baseline  {c.baseline:.3f}\n  candidate {c.candidate:.3f}")
    console.print(f"  difference {d.mean:+.3f}  (95% CI {d.low:+.3f} to {d.high:+.3f})")
    console.print(f"  McNemar p = {c.p_value:.3g}")
    console.print(f"  verdict: [bold]{c.verdict}[/bold]")
    if c.same_snapshot is False:
        console.print(
            "  [yellow]note: the runs used different docs snapshots; scores are not strictly "
            "comparable (ADR 0005)[/yellow]"
        )
    if c.only_baseline_passed:
        console.print(f"  newly failing: {', '.join(c.only_baseline_passed)}")
    if c.only_candidate_passed:
        console.print(f"  newly passing: {', '.join(c.only_candidate_passed)}")


@app.command()
def power(
    delta: Annotated[float, typer.Option(help="Pass-rate change to detect, e.g. 0.10.")] = 0.10,
    cases: Annotated[
        int | None, typer.Option(help="Also show the smallest change this many cases detect.")
    ] = None,
) -> None:
    """How many paired test cases detect a change in pass rate (80% power, 5% two-sided)."""
    from agent_evals.stats.power import cases_for_paired_delta, detectable_delta

    table = Table(show_header=True, header_style="bold")
    table.add_column("discordance", justify="right")
    table.add_column(f"cases to detect {delta:+.2f}", justify="right")
    if cases is not None:
        table.add_column(f"detectable with {cases} cases", justify="right")
    for d in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
        if d < delta:
            continue
        row = [f"{d:.0%}", str(cases_for_paired_delta(delta, d))]
        if cases is not None:
            smallest = detectable_delta(cases, d)
            row.append(f"{smallest:+.3f}" if smallest <= d else "nothing: needs more cases")
        table.add_row(*row)
    console.print(table)
    console.print(
        "Discordance: share of cases where exactly one of the two agents passes. Unknown until "
        "two agents are compared; 15-25% is typical for two prompt versions of one model."
    )


def _print_summary(store: ResultsStore, run_id: str) -> None:
    from agent_evals.report import LOWER_IS_BETTER, NEUTRAL, summarize

    s = summarize(store, run_id)
    info = s.info
    console.print(
        f"\n[bold]{run_id}[/bold]  agent={info['agent']}@{info['agent_version']}  "
        f"suite={info['suite']}  status={info['status']}"
    )
    table = Table(show_header=True, header_style="bold")
    for col in ("metric", "mean", "95% CI", "interval", "cases", "better"):
        table.add_column(col, justify="right" if col != "metric" else "left")
    for name, est in s.metrics.items():
        table.add_row(
            name,
            f"{est.mean:.3f}",
            f"{est.low:.3f} to {est.high:.3f}",
            est.method,
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
