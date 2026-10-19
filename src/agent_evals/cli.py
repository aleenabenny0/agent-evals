"""Command-line interface: `evals snapshot | validate | run | report | compare`."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from agent_evals.config import Config, load_config

if TYPE_CHECKING:
    from agent_evals.stats.core import Estimate
    from agent_evals.store.db import ResultsStore
    from agent_evals.types import Case, Grade

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="YAML config file.")]
DEFAULT_CONFIG = Path("configs/default.yaml")


def _config(path: Path) -> Config:
    return load_config(path if path.exists() else None)


def _require_unchanged_cases(info: dict[str, Any], suite_path: Path) -> None:
    """An online run's cases file must be the one it ran: a day regenerated since would put
    other questions under the same ids. (A suite file may change on purpose; see regrade.)"""
    from agent_evals.harness.manifest import file_sha256

    recorded = info["manifest"].get("suite", {}).get("sha256")
    if info.get("online") and suite_path.exists() and file_sha256(suite_path) != recorded:
        console.print(
            f"[red]{suite_path} has changed since online run {info['run_id']}; its answers "
            "belong to other questions now.[/red]"
        )
        raise typer.Exit(code=2)


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
    restore_id: Annotated[
        str | None,
        typer.Option("--restore", help="Move this case id back from the rejected file; no review."),
    ] = None,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Walk through unverified drafts one by one, with the support quote shown in its page."""
    import sys

    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.text import Text

    from agent_evals.corpus import Corpus
    from agent_evals.review import (
        accept,
        answer_terms,
        quote_context,
        reject,
        restore,
        term_spans,
        uncovered_terms,
    )
    from agent_evals.suites import load_suite, write_suite

    def mark(segment: str, terms: list[str]) -> Text:
        """Page text with every answer fact (number or required phrase) in cyan."""
        t = Text(segment)
        for a, b in term_spans(segment, terms):
            t.stylize("bold cyan", a, b)
        return t

    base = suite.name.removesuffix(".jsonl").removesuffix(".drafts")
    rejected_path = suite.with_name(f"{base}.rejected.jsonl")
    if restore_id is not None:
        rejected = load_suite(rejected_path) if rejected_path.exists() else []
        found = [c for c in rejected if c.id == restore_id]
        if not found:
            console.print(f"[red]{restore_id!r} is not in {rejected_path}.[/red]")
            raise typer.Exit(code=2)
        write_suite(suite, [*load_suite(suite), restore(found[0])])
        left = [c for c in rejected if c.id != restore_id]
        if left:
            write_suite(rejected_path, left)
        else:
            rejected_path.unlink()
        console.print(f"[green]{restore_id} restored to {suite} as an unverified draft.[/green]")
        return

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
    rejected = load_suite(rejected_path) if rejected_path.exists() else []
    tally = {"accepted": 0, "rejected": 0, "skipped": 0}

    for i, c in enumerate(drafts, 1):
        gold = c.gold
        if gold is None:  # drafts are offline cases, which always have gold
            continue
        console.rule(f"{c.id}   {c.type}, {c.area}   {i}/{len(drafts)}")
        console.print(Panel(c.question, title="question"))
        console.print(Panel(gold.answer, title="gold answer"))
        console.print(f"must_include: {gold.must_include}")
        for url in gold.evidence_urls:
            console.print(f"evidence: {url}")
        terms = answer_terms(c)
        missing = uncovered_terms(c)
        if missing:
            console.print("[cyan]answer facts with no support quote: " + ", ".join(missing) + "[/]")
        console.print("[dim]yellow = support quote; cyan = other answer facts on the page[/dim]")
        for n, quote in enumerate(gold.support, 1):
            shown = False
            if corpus is not None:
                for url in gold.evidence_urls:
                    page = corpus.pages.get(url)
                    ctx = quote_context(page.text, quote) if page else None
                    if ctx:
                        before, match, after = ctx
                        body = Text.assemble(
                            mark(before, terms), (match, "bold yellow"), mark(after, terms)
                        )
                        title = f"support {n}/{len(gold.support)}, in context of {url}"
                        console.print(Panel(body, title=title))
                        shown = True
                        break
            if not shown:
                console.print(Panel(quote, title=f"support {n}/{len(gold.support)}"))
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
                reason = Prompt.ask("reason (at least a few words; empty cancels)").strip()
            except EOFError:
                reason = ""
            if len(reason) < 4:
                console.print("[yellow]Rejection cancelled; the case stays a draft.[/yellow]")
                tally["skipped"] += 1
                continue
            rejected.append(reject(c, reviewer, reason))
            cases = [x for x in cases if x.id != c.id]
            tally["rejected"] += 1
            console.print(
                f"rejected; undo with: evals review {suite} --reviewer {reviewer} --restore {c.id}"
            )
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
    from agent_evals.suites import (
        load_suite,
        paraphrase_groups,
        stratified_split,
        unverified_drafts,
        write_suite,
    )

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
    if groups := paraphrase_groups(cases):
        n = len(groups) + sum(len(m) for m in groups.values())
        console.print(f"{n} cases in {len(groups)} paraphrase group(s) placed in dev")
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
) -> str:
    """Run an agent on a suite, grade every attempt, and store the results."""
    load_dotenv()
    from agent_evals.agents.docs.agent import DocsAgent
    from agent_evals.agents.docs.baselines import ClosedBookAgent, OracleEvidenceAgent
    from agent_evals.corpus import Corpus
    from agent_evals.graders.deterministic import (
        AnswerGrader,
        CitationGrader,
        FormatGrader,
        GroundingGrader,
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
        GroundingGrader(corpus),
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
        _print_summary(store, run_id, cfg.judge.grader_name)
    finally:
        store.close()
    return run_id


@app.command()
def report(
    run_id: Annotated[str | None, typer.Argument(help="Defaults to the latest run.")] = None,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Summarize a run: every metric with a 95% confidence interval."""
    from agent_evals.store.db import ResultsStore

    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        rid = run_id or store.latest_run_id()
        if rid is None:
            console.print("No runs yet. Try `evals run suites/docs/smoke.jsonl`.")
            raise typer.Exit(code=1)
        _print_summary(store, rid, cfg.judge.grader_name)
    finally:
        store.close()


@app.command()
def trace(
    run_id: str,
    case_id: str,
    attempt: Annotated[int, typer.Option(min=1, help="Attempt number.")] = 1,
    chars: Annotated[int, typer.Option(help="Characters of each tool input/output to show.")] = 400,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Print one stored trajectory step by step, then the final output and its grades."""
    from rich.panel import Panel

    from agent_evals.store.db import ResultsStore

    store = ResultsStore(_config(config).paths.db)
    try:
        rec = store.attempt(run_id, case_id, attempt)
    finally:
        store.close()
    if rec is None:
        console.print(f"[red]No attempt {attempt} for {case_id} in {run_id}.[/red]")
        raise typer.Exit(code=1)
    console.rule(f"{run_id}  {case_id}  attempt {attempt}")
    for step in rec["trajectory"]:
        if step["kind"] == "model":
            blocks = step.get("output", {}).get("content", [])
            kinds = ", ".join(b.get("name") or b.get("type", "?") for b in blocks)
            cached = " (cache)" if step.get("cache_hit") else ""
            console.print(f"[bold]{step['index']}. model{cached}[/bold] -> {kinds}")
            for b in blocks:
                if b.get("type") == "text" and b.get("text"):
                    console.print(f"     text: {b['text'][:chars]!r}")
        else:
            flag = " [red]error[/red]" if step.get("is_error") else ""
            console.print(f"[bold]{step['index']}. tool {step['name']}[/bold]{flag}")
            console.print(f"     input:  {str(step.get('input'))[:chars]}")
            console.print(f"     output: {str(step.get('output'))[:chars]}")
    if rec["error"]:
        console.print(Panel(rec["error"], title="agent error", style="red"))
    if rec["output"]:
        out = rec["output"]
        console.print(Panel(str(out.get("answer", ""))[: chars * 3], title="answer"))
        for cit in out.get("citations", []) or []:
            console.print(f"  cite: {cit.get('url')} :: {str(cit.get('quote'))[:chars]!r}")
        console.print(f"  abstained={out.get('abstained')} confidence={out.get('confidence')}")
    grades = Table(show_header=True, header_style="bold", title="grades")
    for col in ("grader", "metric", "value", "passed", "reason"):
        grades.add_column(col)
    for g in rec["grades"]:
        grades.add_row(g["grader"], g["metric"], f"{g['value']:.3g}", str(g["passed"]), g["reason"])
    console.print(grades)
    console.print(
        f"{rec['model_calls']} model calls ({rec['cached_calls']} cached), "
        f"{rec['input_tokens']} in / {rec['output_tokens']} out tokens, {rec['latency_ms']:.0f} ms"
        + (f", nominal cost ${rec['cost_usd']:.4f}" if rec["cost_usd"] is not None else "")
    )


@app.command()
def judge(
    run_id: str,
    limit: Annotated[int | None, typer.Option(help="Judge at most N answers.")] = None,
    salt: Annotated[
        str, typer.Option(help="Cache salt. Change it to re-sample the judge (stability probe).")
    ] = "judge",
    replace: Annotated[
        bool, typer.Option("--replace", help="Overwrite this run's existing judge grades.")
    ] = False,
    only_labeled: Annotated[
        str | None,
        typer.Option(
            "--only-labeled",
            help="Judge only answers that carry hand labels on this side (judge-dev or "
            "judge-test): the cheap loop for tuning the judge prompt. Replaces just those grades.",
        ),
    ] = None,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Judge a run's stored answers (supported? responsive?) with the LLM judge. Costs money."""
    from concurrent.futures import ThreadPoolExecutor

    from rich.progress import Progress

    from agent_evals.graders.judge import JudgeGrader
    from agent_evals.harness.cache import CallCache
    from agent_evals.harness.llm import AnthropicClient
    from agent_evals.harness.runner import grade_all
    from agent_evals.online import ONLINE_JUDGE_VERSION
    from agent_evals.store.db import ResultsStore
    from agent_evals.suites import load_suite
    from agent_evals.types import AgentResult

    load_dotenv()
    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        info = store.run_info(run_id)
        grader = JudgeGrader(AnthropicClient(CallCache(cfg.paths.cache_dir)), cfg.judge, salt=salt)
        if info["online"] and grader.version != ONLINE_JUDGE_VERSION:
            console.print(
                f"[red]An online run is judged only by the validated judge "
                f"{ONLINE_JUDGE_VERSION}; this config's judge is {grader.version}.[/red]"
            )
            raise typer.Exit(code=2)
        suite_path = Path(str(info["manifest"].get("suite", {}).get("path", "")))
        if not suite_path.exists():
            console.print(f"[red]The run's suite file {suite_path} is gone; cannot judge.[/red]")
            raise typer.Exit(code=2)
        _require_unchanged_cases(info, suite_path)
        cases = {c.id: c for c in load_suite(suite_path)}
        todo = [a for a in store.answered_attempts(run_id) if a["case_id"] in cases]
        if only_labeled is not None:
            labeled = {
                (x["case_id"], x["attempt"])
                for x in store.labels(run_id)
                if x["label_split"] == only_labeled
            }
            todo = [a for a in todo if (a["case_id"], a["attempt"]) in labeled]
            keys = [(a["case_id"], a["attempt"]) for a in todo]
            store.delete_grades(run_id, cfg.judge.grader_name, keys)
        else:
            if store.grade_count(run_id, cfg.judge.grader_name) and not replace:
                console.print(
                    f"[red]This run already has {cfg.judge.grader_name} grades; pass "
                    "--replace.[/red]"
                )
                raise typer.Exit(code=2)
            store.delete_grades(run_id, cfg.judge.grader_name)
        todo = todo[:limit]

        def one(a: dict[str, object]) -> tuple[dict[str, object], list[tuple[str, Grade]]]:
            case = cases[str(a["case_id"])]
            result = AgentResult(output=a["output"])  # type: ignore[arg-type]
            return a, grade_all([grader], case, result)

        judged = 0
        with ThreadPoolExecutor(max_workers=cfg.run.workers) as pool, Progress() as progress:
            task = progress.add_task("Judging", total=len(todo))
            for a, graded in pool.map(one, todo):
                store.add_grades(run_id, str(a["case_id"]), int(str(a["attempt"])), graded)
                judged += 1
                progress.advance(task)
        store.annotate_run(
            run_id,
            cfg.judge.grader_name,
            {
                "version": grader.version,
                "model": cfg.judge.model.name,
                "rubric": cfg.judge.rubric_version,
                "salt": salt,
            },
        )
        u = grader.usage
        cost = u.cost_usd(cfg.judge.model.pricing)
        uncached = (cost or 0.0) * (u.model_calls - u.cached_calls) / max(u.model_calls, 1)
        spent = f"; about ${uncached:.2f} spent" if cost is not None else ""
        console.print(
            f"judged {judged} answers with {cfg.judge.model.name} (judge {grader.version}): "
            f"{u.model_calls} calls ({u.cached_calls} from cache), {u.input_tokens} in / "
            f"{u.output_tokens} out tokens{spent}"
        )
        _print_summary(store, run_id, cfg.judge.grader_name)
    finally:
        store.close()


@app.command()
def label(
    run_id: str,
    labeler: Annotated[str, typer.Option(help="Your name; stored with each label.")],
    limit: Annotated[int | None, typer.Option(help="Label at most N answers this time.")] = None,
    attempt: Annotated[int, typer.Option(min=1, help="Which attempt of each case to label.")] = 1,
    revisit: Annotated[
        str | None,
        typer.Option(
            "--revisit",
            help="Walk your existing labels instead of new answers: 'unnoted' (no note yet), "
            "'pending' (not yet redone under the two-question rubric) or 'all'. Enter keeps a "
            "previous answer; the note can be added or changed.",
        ),
    ] = None,
    split: Annotated[
        str | None,
        typer.Option(help="With --revisit: only labels on this side, judge-dev or judge-test."),
    ] = None,
    show_judge: Annotated[
        bool,
        typer.Option(
            "--show-judge",
            help="Show the judge's verdicts and reasons for each answer (calibration). Allowed on "
            "judge-dev only: judge-test labels must never be made with the judge in view.",
        ),
    ] = False,
    case_ids: Annotated[
        list[str] | None,
        typer.Option("--case", help="Only these case ids (repeat the flag); re-open just them."),
    ] = None,
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="With --revisit: hide previous answers and notes, ask everything anew (for a "
            "blind pass over labels made under an older rubric). Keeps each label's split.",
        ),
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Hand-label stored answers for judge validation: supported by its quotes? responsive?"""
    import sys

    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.text import Text

    from agent_evals.corpus import Corpus
    from agent_evals.graders.judge_prompts import SUPPORT_RUBRICS
    from agent_evals.judge_validation import disagreements, label_split, revisit_targets
    from agent_evals.review import quote_context
    from agent_evals.store.db import ResultsStore
    from agent_evals.suites import load_suite

    if not sys.stdin.isatty():
        console.print("[red]evals label needs an interactive terminal.[/red]")
        raise typer.Exit(code=2)
    if show_judge and split != "judge-dev":
        console.print(
            "[red]--show-judge needs --revisit ... --split judge-dev: the judge-test "
            "labels are the judge's exam and must be made without seeing it.[/red]"
        )
        raise typer.Exit(code=2)
    cfg = _config(config)
    corpus = None
    if (cfg.paths.corpus_dir / "pages.jsonl").exists():
        corpus = Corpus.load(cfg.paths.corpus_dir)
    store = ResultsStore(cfg.paths.db)
    try:
        info = store.run_info(run_id)
        suite_path = Path(str(info["manifest"].get("suite", {}).get("path", "")))
        _require_unchanged_cases(info, suite_path)
        cases = {c.id: c for c in load_suite(suite_path)} if suite_path.exists() else {}
        mine = store.labels(run_id, labeler)
        previous = {(x["case_id"], x["attempt"]): x for x in mine}
        judge_view = store.grade_reasons(run_id, cfg.judge.grader_name) if show_judge else {}
        answered = [a for a in store.answered_attempts(run_id) if a["case_id"] in cases]
        if case_ids:
            wanted = set(case_ids)
            answered = [a for a in answered if a["case_id"] in wanted]
            mine = [x for x in mine if x["case_id"] in wanted]
        if revisit is not None:
            by_key = {(a["case_id"], a["attempt"]): a for a in answered}
            targets = revisit_targets(mine, revisit)
            if split is not None:
                targets = [x for x in targets if x["label_split"] == split]
            if revisit == "disagree":
                if not show_judge:
                    console.print(
                        "[red]--revisit disagree needs --show-judge (and so judge-dev): it walks "
                        "the labels the judge disputes.[/red]"
                    )
                    raise typer.Exit(code=2)
                targets = disagreements(targets, judge_view)
            todo = [by_key[k] for x in targets if (k := (x["case_id"], x["attempt"])) in by_key]
            todo = todo[:limit]
            if not todo:
                console.print(f"Nothing to revisit: no '{revisit}' labels by {labeler} here.")
                return
        else:
            todo = [
                a
                for a in answered
                if a["attempt"] == attempt and (a["case_id"], attempt) not in previous
            ][:limit]
            if not todo:
                console.print(
                    f"Nothing to label: {len(previous)} already labeled by {labeler} in this run."
                )
                return
        tally = {"judge-dev": 0, "judge-test": 0}
        yn = {True: "y", False: "n"}
        for i, a in enumerate(todo, 1):
            c = cases[a["case_id"]]
            out = a["output"]
            prev = previous.get((c.id, a["attempt"])) if revisit is not None else None
            if fresh:
                prev = None  # asked anew; the stored split is kept by label_split(c.id) below
            console.rule(f"{c.id} #{a['attempt']}   {c.type}, {c.area}   {i}/{len(todo)}")
            if prev is not None:
                core_prev = prev.get("core_supported")
                full_prev = prev.get("supported")
                console.print(
                    f"[cyan]your labels so far: "
                    f"core={yn[core_prev] if isinstance(core_prev, bool) else '-'} "
                    f"fully={yn[full_prev] if isinstance(full_prev, bool) else '-'} "
                    f"responsive={yn[prev['responsive']]} note={prev['note'] or '(none)'}; "
                    "Enter keeps an answer[/cyan]"
                )
            for metric, (value, reason) in sorted(judge_view.get((c.id, a["attempt"]), {}).items()):
                verdict = "pass" if value >= 1.0 else "FAIL"
                console.print(f"[magenta]judge {metric} = {verdict}: {reason}[/magenta]")
            console.print(Panel(c.question, title="question"))
            console.print(Panel(str(out.get("answer", "")), title="assistant's answer"))
            cits = [x for x in (out.get("citations") or []) if isinstance(x, dict)]
            if cits:
                # What the judge sees: the cited text alone. Shown first and whole, so that
                # support is judged against the citation and not against the page around it.
                evidence = Text()
                for n, cit in enumerate(cits, 1):
                    evidence.append(f"{n}. ", style="bold")
                    evidence.append(str(cit.get("quote", "")), style="bold yellow")
                    evidence.append("\n")
                console.print(
                    Panel(
                        evidence,
                        title="THE CITED QUOTES, exactly as the agent submitted them. Only this "
                        "text counts as evidence; the page context below does not.",
                    )
                )
            for n, cit in enumerate(cits, 1):
                url, quote = str(cit.get("url", "")), str(cit.get("quote", ""))
                page = corpus.pages.get(url) if corpus is not None else None
                ctx = quote_context(page.text, quote, width=200) if page else None
                if ctx:
                    before, match, after = ctx
                    body = Text.assemble((before, "dim"), (match, "bold yellow"), (after, "dim"))
                    title = f"page context for citation {n} (dim text is NOT cited): {url}"
                    console.print(Panel(body, title=title, style="dim"))
                else:
                    # The grader has the final word on whether a quote is on the page; the
                    # screen must never contradict it.
                    if page is None:
                        note, style = "page not in snapshot", "yellow"
                    elif corpus is not None and corpus.contains_quote(url, quote):
                        note, style = "on the page per the grader; shown without context", "yellow"
                    else:
                        note, style = "quote NOT found on page (grader agrees)", "red"
                    title = f"citation {n}/{len(cits)}: {url} ({note})"
                    console.print(Panel(quote, title=title, style=style))
            supported: bool | None = None
            core_supported: bool | None = None
            if cits:
                # The rubric is hard-wrapped for the prompt file; let the panel wrap it instead.
                rubric_text = SUPPORT_RUBRICS[cfg.judge.rubric_version]
                rubric = "\n\n".join(" ".join(p.split()) for p in rubric_text.split("\n\n"))
                rubric += (
                    "\n\nFor you, not the judge: judge against the cited quotes panel above. A "
                    "header row, a neighbouring table row or a sentence that is visible only in the"
                    " dim page context was not cited and does not support anything."
                )
                console.print(Panel(rubric, title="rubric (the judge reads the same text)"))
                prev_core = prev.get("core_supported") if prev is not None else None
                prev_c = yn[prev_core] if isinstance(prev_core, bool) else None
                question = (
                    "core supported: are the claims that ANSWER the question backed by the quotes? "
                    "(y)es, (n)o, (s)kip, (q)uit"
                )
                if prev_c is not None:
                    ans = Prompt.ask(question, choices=["y", "n", "s", "q"], default=prev_c)
                else:
                    ans = Prompt.ask(question, choices=["y", "n", "s", "q"])
                if ans == "q":
                    break
                if ans == "s":
                    continue
                core_supported = ans == "y"
                if not core_supported:
                    supported = False  # an answer whose core is unsupported is not fully supported
                    console.print("[dim]fully supported: no (follows from core = no)[/dim]")
                else:
                    prev_s = yn.get(prev["supported"]) if prev is not None else None
                    question = (
                        "fully supported: is EVERY claim, context included, backed by the quotes? "
                        "(y)es, (n)o, (s)kip, (q)uit"
                    )
                    if prev_s is not None:
                        ans = Prompt.ask(question, choices=["y", "n", "s", "q"], default=prev_s)
                    else:
                        ans = Prompt.ask(question, choices=["y", "n", "s", "q"])
                    if ans == "q":
                        break
                    if ans == "s":
                        continue
                    supported = ans == "y"
            else:
                console.print(
                    "[dim]no citations: supported is not asked (uncited_answer covers it)[/dim]"
                )
            _show_reference(c)
            question = (
                "responsive: does it answer the question as asked? (y)es, (n)o, (s)kip, (q)uit"
            )
            if prev is not None:
                prev_r = yn[prev["responsive"]]
                ans = Prompt.ask(question, choices=["y", "n", "s", "q"], default=prev_r)
            else:
                ans = Prompt.ask(question, choices=["y", "n", "s", "q"])
            if ans == "q":
                break
            if ans == "s":
                continue
            note = Prompt.ask(
                "note: what was unsupported or off, in your words (optional)",
                default=(prev["note"] or "") if prev is not None else "",
            ).strip()
            split = label_split(c.id)
            store.record_label(
                run_id,
                c.id,
                a["attempt"],
                labeler,
                supported=supported,
                responsive=ans == "y",
                note=note,
                label_split=split,
                core_supported=core_supported,
            )
            tally[split] += 1
        total = store.labels(run_id, labeler)
        verb = "revisited" if revisit is not None else "labeled"
        console.print(
            f"\n{verb} {sum(tally.values())} now ({tally['judge-dev']} judge-dev, "
            f"{tally['judge-test']} judge-test); {len(total)} in total by {labeler} for this run"
        )
    finally:
        store.close()


@app.command("label-export")
def label_export(
    run_id: str,
    labeler: Annotated[str | None, typer.Option(help="Only this person's labels.")] = None,
    out: Annotated[
        Path | None, typer.Option(help="Output file; default results/labels/<run_id>.jsonl.")
    ] = None,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Write a run's hand labels to JSONL so they live in git, not only in the local database."""
    import json

    from agent_evals.store.db import ResultsStore

    store = ResultsStore(_config(config).paths.db)
    try:
        labels = store.labels(run_id, labeler)
    finally:
        store.close()
    path = out or Path("results") / "labels" / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(labels, key=lambda x: (str(x["case_id"]), int(x["attempt"]), str(x["labeler"])))
    path.write_text(
        "".join(json.dumps({"run_id": run_id, **r}, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    console.print(f"wrote {len(rows)} labels to {path}")


@app.command("judge-validate")
def judge_validate(
    run_id: str,
    split: Annotated[
        str, typer.Option(help="Which labels to score against: judge-test, judge-dev or all.")
    ] = "judge-test",
    labeler: Annotated[str | None, typer.Option(help="Only this person's labels.")] = None,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Agreement between hand labels and judge verdicts: kappa, TPR, TNR, confusion counts."""
    from agent_evals.judge_validation import agreement
    from agent_evals.store.db import ResultsStore

    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        labels = store.labels(run_id, labeler)
        verdicts = store.grades_for(run_id, cfg.judge.grader_name)
    finally:
        store.close()
    if split != "all":
        labels = [x for x in labels if x["label_split"] == split]
    title = f"judge vs {labeler or 'labels'} on {split}"
    table = Table(show_header=True, header_style="bold", title=title)
    columns = (
        "verdict",
        "n",
        "kappa",
        "TPR (catches failures)",
        "TNR (passes good answers)",
        "tp/fp/fn/tn",
    )
    for col in columns:
        table.add_column(col, justify="right" if col != "verdict" else "left")
    for verdict in ("core_supported", "supported", "responsive"):
        metric = f"judge_{verdict}"
        pairs = [
            (bool(x[verdict]), verdicts[(x["case_id"], x["attempt"])][metric] >= 1.0)
            for x in labels
            if x.get(verdict) is not None
            and (x["case_id"], x["attempt"]) in verdicts
            and metric in verdicts[(x["case_id"], x["attempt"])]
        ]
        if not pairs:
            table.add_row(verdict, "0", "-", "-", "-", "-")
            continue
        a = agreement(verdict, pairs)
        colour = "green" if a.kappa >= 0.7 else "red"

        def fmt(e: Estimate | None) -> str:
            return "-" if e is None else f"{e.mean:.2f} ({e.low:.2f} to {e.high:.2f})"

        table.add_row(
            verdict,
            str(a.n),
            f"[{colour}]{a.kappa:.3f}[/{colour}]",
            fmt(a.tpr),
            fmt(a.tnr),
            f"{a.tp}/{a.fp}/{a.fn}/{a.tn}",
        )
    console.print(table)
    console.print(
        "bars (docs/plans/llm-judge.md): kappa at least 0.7 on each verdict on judge-test; "
        "fewer than 30 labeled answers is too few to conclude anything"
    )


@app.command("judge-probe")
def judge_probe(
    run_id: str,
    limit: Annotated[int, typer.Option(help="Passed answers to build variants from.")] = 40,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Mutation tests and bias probes for the judge, on answers it passed. Costs money."""
    import json
    from concurrent.futures import ThreadPoolExecutor

    from rich.progress import Progress

    from agent_evals.graders.judge import JudgeGrader
    from agent_evals.harness.cache import CallCache
    from agent_evals.harness.llm import AnthropicClient
    from agent_evals.harness.runner import grade_all
    from agent_evals.judge_probes import MUTANTS, Variant, behaved, report_path, variants
    from agent_evals.stats.core import wilson_ci
    from agent_evals.store.db import ResultsStore
    from agent_evals.suites import load_suite
    from agent_evals.types import AgentResult

    load_dotenv()
    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        info = store.run_info(run_id)
        suite_path = Path(str(info["manifest"].get("suite", {}).get("path", "")))
        cases = {c.id: c for c in load_suite(suite_path)}
        verdicts = store.grades_for(run_id, cfg.judge.grader_name)
        answered = store.answered_attempts(run_id)
    finally:
        store.close()
    if not verdicts:
        console.print("[red]No judge grades on this run yet; run `evals judge` first.[/red]")
        raise typer.Exit(code=2)
    passed_both = [
        a
        for a in answered
        if a["case_id"] in cases
        and verdicts.get((a["case_id"], a["attempt"]), {}).get("judge_supported") == 1.0
        and verdicts.get((a["case_id"], a["attempt"]), {}).get("judge_responsive") == 1.0
    ][:limit]
    pool = [
        (str(c.get("url", "")), str(c.get("quote", "")))
        for a in answered
        for c in (a["output"].get("citations") or [])
        if isinstance(c, dict)
    ]
    llm = AnthropicClient(CallCache(cfg.paths.cache_dir))
    graders = {
        "judge": JudgeGrader(llm, cfg.judge, salt="judge"),
        "probe": JudgeGrader(llm, cfg.judge, salt="probe"),
    }
    jobs = [(a, v) for a in passed_both for v in variants(a["output"], pool)]

    def one(job: tuple[dict[str, Any], Variant]) -> tuple[dict[str, Any], Variant, bool, str]:
        a, v = job
        graded = grade_all([graders[v.salt]], cases[a["case_id"]], AgentResult(output=v.output))
        judged = {g.metric: g.value for _, g in graded if g.metric != "grader_error"}
        ok = behaved(v, verdicts[(a["case_id"], a["attempt"])], judged)
        reasons = "; ".join(f"{g.metric}={g.value:g} {g.reason}" for _, g in graded)
        return a, v, ok, reasons

    tally: dict[str, list[bool]] = {}
    misses: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=cfg.run.workers) as pool_, Progress() as progress:
        task = progress.add_task("Probing the judge", total=len(jobs))
        for a, v, ok, reasons in pool_.map(one, jobs):
            tally.setdefault(v.probe, []).append(ok)
            if not ok:
                misses.append(
                    {
                        "case_id": a["case_id"],
                        "attempt": a["attempt"],
                        "probe": v.probe,
                        "answer": v.output.get("answer"),
                        "judge": reasons,
                    }
                )
            progress.advance(task)

    table = Table(show_header=True, header_style="bold", title=f"judge probes on {run_id}")
    for col in ("probe", "kind", "n", "expected", "observed (95% CI)", "bar"):
        table.add_column(col, justify="right" if col in ("n",) else "left")
    summary: dict[str, Any] = {}
    for probe, oks in tally.items():
        kind = "mutant" if probe in MUTANTS else "probe"
        est = wilson_ci(sum(oks), len(oks))
        bar = 0.90 if kind == "mutant" else 0.95
        colour = "green" if est.mean >= bar else "red"
        expected = "judge fails it" if kind == "mutant" else "verdicts unchanged"
        table.add_row(
            probe,
            kind,
            str(est.n),
            expected,
            f"[{colour}]{est.mean:.2f}[/{colour}] ({est.low:.2f} to {est.high:.2f})",
            f"at least {bar:.2f}",
        )
        summary[probe] = {
            "kind": kind,
            "n": est.n,
            "rate": est.mean,
            "low": est.low,
            "high": est.high,
            "bar": bar,
        }
    console.print(table)
    u = graders["judge"].usage.add(graders["probe"].usage)
    console.print(
        f"{len(passed_both)} passed answers, {len(jobs)} variants, {u.model_calls} judge calls "
        f"({u.cached_calls} from cache), {u.input_tokens} in / {u.output_tokens} out tokens"
    )
    out = report_path(run_id, cfg.judge.grader_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "judge": graders["judge"].version,
                "summary": summary,
                "misses": misses,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    console.print(f"details, including every miss with the judge's reasons: {out}")


@app.command()
def regrade(
    run_id: str,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Re-score a run's stored answers with the code graders against the suite as it is now.

    Free: the agent is not re-run. Use after a case's keyword list or a grader changes. Judge
    grades are left alone.
    """
    from agent_evals.corpus import Corpus
    from agent_evals.graders.deterministic import (
        AnswerGrader,
        CitationGrader,
        FormatGrader,
        GroundingGrader,
        TrajectoryGrader,
    )
    from agent_evals.harness.runner import regrade_run
    from agent_evals.store.db import ResultsStore
    from agent_evals.suites import load_suite
    from agent_evals.types import Grader

    cfg = _config(config)
    corpus = Corpus.load(cfg.paths.corpus_dir)
    graders: list[Grader] = [
        AnswerGrader(),
        CitationGrader(corpus, min_quote_chars=cfg.graders.min_quote_chars),
        FormatGrader(),
        TrajectoryGrader(),
        GroundingGrader(corpus),
    ]
    store = ResultsStore(cfg.paths.db)
    try:
        info = store.run_info(run_id)
        suite_path = Path(str(info["manifest"].get("suite", {}).get("path", "")))
        if not suite_path.exists():
            console.print(f"[red]The run's suite file {suite_path} is gone.[/red]")
            raise typer.Exit(code=2)
        _require_unchanged_cases(info, suite_path)
        n = regrade_run(store, run_id, load_suite(suite_path), graders)
        console.print(f"regraded {n} attempts of {run_id} against {suite_path}")
        _print_summary(store, run_id, cfg.judge.grader_name)
    finally:
        store.close()


@app.command("gate")
def gate_cmd(
    champion: str,
    challenger: str,
    target: Annotated[
        str, typer.Option(help="Metric the change is meant to improve.")
    ] = "judge_supported",
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """The ADR 0008 gate: target improved, no monitored metric regressed, no category regressed."""
    from agent_evals.report import gate
    from agent_evals.store.db import ResultsStore
    from agent_evals.suites import load_suite

    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        if store.run_info(champion)["online"] or store.run_info(challenger)["online"]:
            console.print(
                "[red]The gate compares suite runs, which have reference answers; online runs "
                "have none and are watched by the monitor instead (phase 7 part 3).[/red]"
            )
            raise typer.Exit(code=2)
        manifest = store.run_info(champion)["manifest"]
        suite_path = Path(str(manifest.get("suite", {}).get("path", "")))
        if "test" in suite_path.name:
            console.print(
                "[red]The gate reads the dev split; test is run once after it passes.[/red]"
            )
            raise typer.Exit(code=2)
        cases = load_suite(suite_path) if suite_path.exists() else []
        try:
            g = gate(store, champion, challenger, cases, target, cfg.judge.grader_name)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    finally:
        store.close()

    title = f"paired on the shared dev cases (judge metrics from '{cfg.judge.grader_name}')"
    table = Table(show_header=True, header_style="bold", title=title)
    for col in ("metric", "champion", "challenger", "difference", "95% CI", "verdict"):
        table.add_column(col, justify="right" if col != "metric" else "left")
    for c in (g.target, *g.monitored):
        colour = {"improved": "green", "regressed": "red"}.get(c.verdict, "")
        tag = " (target)" if c.metric == target else ""
        table.add_row(
            c.metric + tag,
            f"{c.baseline:.3f}",
            f"{c.candidate:.3f}",
            f"{c.diff.mean:+.3f}",
            f"{c.diff.low:+.3f} to {c.diff.high:+.3f}",
            f"[{colour}]{c.verdict}[/{colour}]" if colour else c.verdict,
        )
    console.print(table)
    if g.skipped:
        console.print(f"not compared (absent from one run): {', '.join(g.skipped)}")
    cats = Table(show_header=True, header_style="bold", title="correct by category, paired")
    for col in ("category", "n", "difference", "95% CI", ""):
        cats.add_column(col, justify="right" if col in ("n",) else "left")
    for cat in g.categories:
        cats.add_row(
            f"{cat.kind}={cat.name}",
            str(cat.n),
            f"{cat.diff.mean:+.3f}",
            f"{cat.diff.low:+.3f} to {cat.diff.high:+.3f}",
            "[red]regressed[/red]"
            if cat.regressed
            else ("[green]improved[/green]" if cat.diff.low > 0 else ""),
        )
    console.print(cats)
    if g.passed:
        console.print(
            f"[bold green]GATE PASSED[/bold green]: {target} improved, nothing regressed."
        )
    else:
        reasons = []
        if g.target.verdict != "improved":
            reasons.append(f"{target}: {g.target.verdict}")
        reasons += [f"{m} regressed" for m in g.regressed_metrics]
        reasons += [f"{c} regressed on correct" for c in g.regressed_categories]
        console.print("[bold red]GATE FAILED[/bold red]: " + "; ".join(reasons))
        raise typer.Exit(code=1)


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

    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        c = compare(store, baseline, candidate, metric, cfg.judge.grader_name)
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


TRAFFIC_DIR = Path("results/online/traffic")


@app.command()
def simulate(
    day: int,
    model: Annotated[str, typer.Option(help="Generator model.")] = "claude-sonnet-5-5",
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing day whose questions differ.")
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Generate one simulated day of online traffic (phase 7): a generator call per question, a
    checker call per lookup and comparison, and any retries. Costs about $0.65 a day."""
    import dataclasses
    import json

    from agent_evals.corpus import Corpus
    from agent_evals.harness.cache import CallCache, sha256_text
    from agent_evals.harness.llm import AnthropicClient
    from agent_evals.simulate import (
        AREA_MAP_PATH,
        EXTRA_OUTSIDE_SERVICES,
        GENERATOR_VERSION,
        day_summary,
        generate_day,
        load_area_map,
        outside_services,
        page_areas,
        plan_day,
        protected_pages,
    )
    from agent_evals.suites import load_suite

    load_dotenv()
    cfg = _config(config)
    corpus = Corpus.load(cfg.paths.corpus_dir)
    test_path = cfg.paths.suites_dir / "docs" / "test.jsonl"
    test_cases = load_suite(test_path)
    protected = protected_pages(corpus, test_cases)
    excluded = set().union(*protected.values())
    pool = sorted(set(corpus.pages) - excluded)
    areas = page_areas(load_area_map(AREA_MAP_PATH), pool)
    candidates = [*cfg.suites.held_out_services, *EXTRA_OUTSIDE_SERVICES]
    services, dropped = outside_services(candidates, corpus, [c.question for c in test_cases])
    slots = plan_day(day, corpus, excluded, services)
    llm = AnthropicClient(CallCache(cfg.paths.cache_dir))
    questions, redo = generate_day(llm, slots, corpus, areas, model, pool)
    traffic = "".join(q.model_dump_json() + "\n" for q in questions)
    meta = day_summary(questions) | {
        "day": day,
        "generator": model,
        "generator_version": GENERATOR_VERSION,
        "traffic_sha256": sha256_text(traffic),
        "inputs": {
            "corpus": corpus.fingerprint,
            "test_suite": sha256_text(test_path.read_text(encoding="utf-8")),
            "area_map": sha256_text(AREA_MAP_PATH.read_text(encoding="utf-8")),
            "plan": sha256_text(json.dumps([dataclasses.asdict(s) for s in slots])),
        },
        "allowed_source_pages": len(pool),
        "excluded_pages": {reason: len(urls) for reason, urls in protected.items()},
        "outside_services_dropped": dropped,
        "retries": redo,
    }
    traffic_path = TRAFFIC_DIR / f"day-{day:02d}.jsonl"
    meta_path = TRAFFIC_DIR / f"day-{day:02d}.meta.json"
    if meta_path.exists() and not force:
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        if old.get("traffic_sha256") != meta["traffic_sha256"]:
            recorded = old.get("inputs")
            if recorded is None:
                why = "the existing day records no input hashes"
            else:
                changed = [k for k, v in meta["inputs"].items() if recorded.get(k) != v]
                why = "inputs changed: " + (", ".join(changed) or "none, so the generator did")
            console.print(
                f"[red]Day {day} already exists with different questions ({why}). Nothing was "
                "written; pass --force to replace it.[/red]"
            )
            raise typer.Exit(code=1)
    TRAFFIC_DIR.mkdir(parents=True, exist_ok=True)
    traffic_path.write_text(traffic, encoding="utf-8")
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    console.print_json(json.dumps(meta))


def _show_reference(c: Case) -> None:
    """The label screen's reference for the responsive question. An online case has none; a
    false premise shows the page sentence it wrongly contradicts instead."""
    from rich.panel import Panel

    if c.is_online or c.gold is None:
        console.print(
            "[dim]online case: no reference answer, so judge responsive from the question "
            "alone[/dim]"
        )
        if c.gold is not None and c.gold.support:
            console.print(Panel(c.gold.support[0], title="the page sentence it contradicts"))
        return
    console.print(Panel(c.gold.answer, title="reference answer (gold)"))
    console.print(f"must_include: {c.gold.must_include}")


def _interactive() -> bool:
    import sys

    return sys.stdin.isatty()


@app.command("online-run")
def online_run(
    day: Annotated[int, typer.Argument(help="Simulated day to run.")],
    judge_answers: Annotated[
        bool,
        typer.Option("--judge/--no-judge", help="Judge the answers with the pinned judge."),
    ] = True,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Run the docs agent on one simulated day, grade what needs no reference answer, and judge it
    with the pinned judge (phase 7 part 2). Costs money. The run is never the latest run. Any
    config may run (part 5's fault and shadow days need others); the run records which, and
    whether it was the champion's (configs/default.yaml)."""
    from agent_evals.graders.judge import JudgeGrader
    from agent_evals.harness.llm import AnthropicClient
    from agent_evals.online import (
        ONLINE_CASES_DIR,
        ONLINE_JUDGE_VERSION,
        online_case,
        online_suite_name,
    )
    from agent_evals.simulate import load_traffic
    from agent_evals.store.db import ResultsStore
    from agent_evals.suites import write_suite

    cfg = _config(config)
    if judge_answers:  # refused before the agent is paid for, not after
        version = JudgeGrader(AnthropicClient(), cfg.judge).version
        if version != ONLINE_JUDGE_VERSION:
            console.print(
                f"[red]An online run is judged only by the validated judge "
                f"{ONLINE_JUDGE_VERSION}; this config's judge is {version}. Nothing was run.[/red]"
            )
            raise typer.Exit(code=2)
    traffic_path = TRAFFIC_DIR / f"day-{day:02d}.jsonl"
    if not traffic_path.exists():
        console.print(
            f"[red]No simulated traffic for day {day} at {traffic_path}.[/red] Generate it "
            f"first: uv run evals simulate {day}"
        )
        raise typer.Exit(code=2)
    cases_path = ONLINE_CASES_DIR / f"{online_suite_name(day)}.jsonl"
    write_suite(cases_path, [online_case(q) for q in load_traffic(traffic_path)])
    run_id = run(
        suite=cases_path, agent="docs", attempts=1, limit=None, allow_drafts=False, config=config
    )
    store = ResultsStore(cfg.paths.db)
    try:
        champion = config.resolve() == DEFAULT_CONFIG.resolve()
        role = {"day": day, "config": str(config), "champion": champion}
        store.annotate_run(run_id, "online", role)
    finally:
        store.close()
    if judge_answers:
        judge(
            run_id=run_id, limit=None, salt="judge", replace=False, only_labeled=None, config=config
        )


@app.command("pilot-review")
def pilot_review(
    day: Annotated[int, typer.Argument(help="Simulated day to review.")] = 0,
    reviewer: Annotated[str, typer.Option(help="Your name; stored with each answer.")] = "aleena",
    redo: Annotated[
        str, typer.Option(help="Comma-separated question ids to ask again, even if answered.")
    ] = "",
    quick: Annotated[
        bool,
        typer.Option(
            "--quick",
            help="The 10-question realism read of a revised generator: realism only.",
        ),
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Review 30 simulated questions, 6 per persona across all four intents: would a real
    person ask it, and could the snapshot answer it? Each answer is saved as you give it, so you
    can stop with q and rerun the same command to continue; --redo asks chosen ones again, and
    the newer answer replaces the older."""
    import json

    from rich.markup import escape
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.text import Text

    from agent_evals.corpus import Corpus
    from agent_evals.simulate import (
        QUICK_PER_PERSONA,
        append_verdict,
        current_verdicts,
        load_traffic,
        load_verdicts,
        pilot_sample,
        pilot_summary,
        question_sha,
        quick_summary,
        source_passages,
    )

    if not _interactive():
        console.print("[red]evals pilot-review needs an interactive terminal.[/red]")
        raise typer.Exit(code=2)
    cfg = _config(config)
    project = Path(__file__).resolve().parents[2]
    if not (cfg.paths.corpus_dir / "pages.jsonl").exists():
        console.print(
            f"[red]No docs snapshot at {escape(str(cfg.paths.corpus_dir))}. Run this from the "
            f"project folder, the one with pyproject.toml:[/red]\n  cd {project.as_posix()}"
        )
        raise typer.Exit(code=2)
    traffic_path = TRAFFIC_DIR / f"day-{day:02d}.jsonl"
    if not traffic_path.exists():
        console.print(
            f"[red]No simulated traffic for day {day} at {escape(str(traffic_path))}.[/red] "
            f"Generate it first: uv run evals simulate {day}"
        )
        raise typer.Exit(code=2)
    corpus = Corpus.load(cfg.paths.corpus_dir)
    traffic = load_traffic(traffic_path)
    if quick:
        picked = pilot_sample(traffic, day, QUICK_PER_PERSONA, name="quick")
    else:
        picked = pilot_sample(traffic, day)
    again = {s.strip() for s in redo.split(",") if s.strip()}
    if unknown := sorted(again - {q.id for q in picked}):
        console.print(f"[red]Not in the pilot sample: {escape(', '.join(unknown))}[/red]")
        raise typer.Exit(code=2)
    review = "quick" if quick else "pilot"
    out_path = TRAFFIC_DIR.parent / f"{review}-day-{day:02d}.jsonl"
    verdicts = load_verdicts(out_path)
    for i, q in enumerate(picked, 1):
        earlier = current_verdicts([q], verdicts).get(q.id)
        if earlier is not None and q.id not in again:
            continue
        console.rule(f"{i}/{len(picked)}   {q.persona}, {q.intent}")
        # Generated and page text is printed as plain text: rich reads "[error]" as markup and
        # drops it from the screen, and a stray "[/dim]" crashes the review.
        console.print(Panel(Text(q.question), title="simulated question"))
        if earlier is not None:
            before = (
                f"realistic {'y' if earlier['realistic'] else 'n'}, "
                f"answerable {earlier['answerable']}, note: {earlier.get('note') or '(none)'}"
            )
            console.print(f"[dim]Your earlier answer ({escape(q.id)}): {escape(before)}[/dim]")
        if quick:
            pass  # realism only: the question alone, as a real reader would see it
        elif q.intent == "outside_snapshot":
            console.print(
                f"[dim]No snapshot page names {escape(q.service or '')}, so there is no page to "
                "open. The search hits below are the closest the snapshot has: answer n unless "
                "they answer it.[/dim]"
            )
        elif (page := corpus.pages.get(q.source_url or "")) is None:
            console.print(
                f"[yellow]Source page not in the snapshot: {escape(q.source_url or '')}[/yellow]"
            )
        else:
            passages = source_passages(corpus, q)
            body = "\n\n".join(f"{h}: {t}" if h else t for h, t in passages)
            title = escape(f"best-matching passages on its page: {page.title}")
            console.print(Panel(Text(body), title=title))
            console.print(
                f"[dim]These can miss the answer, and another page may have it. The page: "
                f"{escape(q.source_url or '')}. Check it and the search hits below; answer u if "
                "still unsure.[/dim]"
            )
            if q.contradicts:
                premise_title = "the page sentence it should wrongly contradict"
                console.print(Panel(Text(q.contradicts), title=premise_title))
        pages: dict[str, str] = {}
        for hit in corpus.search(q.question, k=10):  # several hits can share a page
            pages.setdefault(hit.url, hit.title)
        hits = "; ".join(list(pages.values())[:3])
        if not quick:
            console.print(f"[dim]top snapshot search hits: {escape(hits)}[/dim]")
        answer_prompt = "Could the snapshot answer it? (y/n/u unsure, q quits)"
        if q.intent == "false_premise":
            answer_prompt = "Could the snapshot show the premise is wrong? (y/n/u unsure, q quits)"
        try:
            realistic = Prompt.ask(
                "Would a real person ask this? (y/n, q quits)",
                choices=["y", "n", "q"],
                case_sensitive=False,
            ).lower()
            if realistic == "q":
                break
            answerable = "skip"  # the realism read asks one question
            if not quick:
                answerable = Prompt.ask(
                    answer_prompt, choices=["y", "n", "u", "q"], case_sensitive=False
                ).lower()
            if answerable == "q":
                break
            note = Prompt.ask(
                "note (optional; Enter skips, q saves and quits)", default="", show_default=False
            )
        except (EOFError, KeyboardInterrupt):  # this question is unsaved; earlier ones are kept
            break
        quit_after = note.strip().lower() == "q"
        record = {
            "id": q.id,
            "question_sha": question_sha(q),
            "persona": q.persona,
            "intent": q.intent,
            "realistic": realistic == "y",
            "answerable": answerable,
            "note": "" if quit_after else note,
            "reviewer": reviewer,
        }
        verdicts[q.id] = record
        append_verdict(out_path, record)
        if quit_after:
            break
    if quick:
        result = quick_summary(picked, verdicts)
        console.print_json(json.dumps(result))
        if not result["complete"]:
            console.print(
                f"Not finished: {result['reviewed']} of {result['sample']} reviewed. Run the "
                "same command to continue."
            )
        elif not result["passes"]:
            console.print(
                f"[red]Fewer than {result['bar']} of {result['sample']} are realistic: the "
                "generator prompt gets revised.[/red]"
            )
            raise typer.Exit(code=1)
        else:
            console.print(
                f"[green]Realism read passed: {result['realistic']} of {result['sample']}.[/green]"
            )
        return
    summary = pilot_summary(picked, verdicts)
    console.print_json(json.dumps(summary))
    if not summary["complete"]:
        console.print(
            f"Not finished: {summary['reviewed']} of {summary['sample']} reviewed. Run the same "
            "command to continue."
        )
    elif summary["meets_two_thirds"] is False:
        console.print(
            "[red]Fewer than two thirds are realistic (weighted by traffic share): the generator "
            "prompts get revised before day 1.[/red]"
        )
        raise typer.Exit(code=1)
    else:
        console.print("[green]Pilot complete: at least two thirds are realistic.[/green]")


def _print_summary(store: ResultsStore, run_id: str, judge: str = "judge") -> None:
    from agent_evals.report import LOWER_IS_BETTER, NEUTRAL, other_judges, summarize

    s = summarize(store, run_id, judge)
    info = s.info
    console.print(
        f"\n[bold]{run_id}[/bold]  agent={info['agent']}@{info['agent_version']}  "
        f"suite={info['suite']}  status={info['status']}"
    )
    judges = store.judge_graders(run_id)
    if judges:
        version = info["manifest"].get(judge, {}).get("version", "not run")
        others = other_judges(store, run_id, judge)
        console.print(
            f"judge metrics from '{judge}' ({version})"
            + (f"; left out: {', '.join(others)} (pick one with -c)" if others else "")
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
    if len(s.pass_at_k) > 1:
        console.print("pass@k: " + "  ".join(f"k={k}: {v:.3f}" for k, v in s.pass_at_k.items()))
    if s.flip_rate is not None:
        f = s.flip_rate
        console.print(
            f"flip rate: {f.mean:.3f} ({f.low:.3f} to {f.high:.3f}, {f.n} cases with repeated "
            "attempts); differences inside this band are noise"
        )
    if s.consistency is not None:
        c = s.consistency
        console.print(
            f"paraphrase consistency: {c.mean:.3f} ({c.low:.3f} to {c.high:.3f}, {c.n} groups)"
        )
    if s.by_area:
        areas = Table(show_header=True, header_style="bold", title="correct by area")
        for col in ("area", "mean", "95% CI", "cases"):
            areas.add_column(col, justify="right" if col != "area" else "left")
        for area, est in s.by_area.items():
            areas.add_row(area, f"{est.mean:.3f}", f"{est.low:.3f} to {est.high:.3f}", str(est.n))
        console.print(areas)
    if s.latency_p50_ms is not None and s.latency_p95_ms is not None:
        console.print(
            f"latency per attempt: p50 {s.latency_p50_ms:.0f} ms, p95 {s.latency_p95_ms:.0f} ms"
        )
    if s.cost_per_success is not None:
        console.print(f"nominal cost per successful attempt: ${s.cost_per_success:.4f}")
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
