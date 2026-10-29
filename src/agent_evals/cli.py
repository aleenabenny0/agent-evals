"""Command-line interface: `evals snapshot | validate | run | report | compare`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from agent_evals.config import Config, JudgeConfig, load_config

if TYPE_CHECKING:
    from agent_evals.harness.llm import LLMClient
    from agent_evals.spend import MeteredLLM, SpendLedger
    from agent_evals.stats.core import Estimate
    from agent_evals.store.db import ResultsStore
    from agent_evals.types import Case, Grade

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="YAML config file.")]
DEFAULT_CONFIG = Path("configs/default.yaml")


def _config(path: Path) -> Config:
    """The config file, which must exist: falling back to defaults from the wrong folder would
    quietly open (and create) another results database."""
    if not path.is_file():
        console.print(
            f"[red]No config file at {path}. Run from the project folder, or pass -c.[/red]"
        )
        raise typer.Exit(code=2)
    return load_config(path)


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

    if not _interactive():
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
    seed: Annotated[
        int, typer.Option(help="Random seed. Record it with the suite (ADR 0006).")
    ] = 0,
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
    console.print(f"seed={seed}; record it with the suite (ADR 0006)")


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
    """Print one stored trajectory step by step, then the final output and its grades. The
    judge's grades on an answer of the pending online judge check are held back."""
    from rich.panel import Panel

    from agent_evals import monitor as mon
    from agent_evals.store.db import ResultsStore

    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        rec = store.attempt(run_id, case_id, attempt)
        held = case_id in mon.blind_cases(store, run_id)  # labeled blind until it is scored
        judges = set(store.judge_graders(run_id))  # every judge, whatever a config calls it
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
        if held and g["grader"] in judges:
            continue
        grades.add_row(g["grader"], g["metric"], f"{g['value']:.3g}", str(g["passed"]), g["reason"])
    console.print(grades)
    if held:
        console.print(
            "[yellow]The judge's grades on this answer are held back until the judge check is "
            "scored: it is labeled blind.[/yellow]"
        )
    console.print(
        f"{rec['model_calls']} model calls ({rec['cached_calls']} cached), "
        f"{rec['input_tokens']} in / {rec['output_tokens']} out tokens, {rec['latency_ms']:.0f} ms"
        + (f", nominal cost ${rec['cost_usd']:.4f}" if rec["cost_usd"] is not None else "")
    )


@dataclass(frozen=True)
class _RoundSpend:
    """A paid claim-judge command's spend in the judge round's ledger (docs/plans/judge-v5.md):
    its meter, checked with an estimate before the first call and held to what the round's cap
    leaves, and the ledger it is recorded in after, however the command ends."""

    step: str
    ledger: SpendLedger
    meter: MeteredLLM

    def record(self, *, done: bool) -> None:
        entry = self.ledger.record(
            self.step if done else f"{self.step} (stopped early)", self.meter
        )
        console.print(
            f"spent ${entry.usd:.4f}: {entry.uncached_calls} of {entry.calls} calls sent to the "
            "API, the rest from the call cache. The judge round has spent "
            f"${self.ledger.total_usd():.2f} of its ${self.ledger.cap_usd:.2f} cap "
            f"({self.ledger.path})."
        )


def _round_spend(step: str, answers: int, llm: LLMClient, judge: JudgeConfig) -> _RoundSpend:
    """The claim judge's calls are the judge round's spend: estimated at the pilot's cost per
    answer (the design's estimate before the pilot), refused (exit 2) before any call when that
    would take the round past its cap, and stopped during the command when the cap is reached."""
    from agent_evals import spend

    ledger = spend.SpendLedger(spend.DEFAULT_PATH)
    estimate = answers * spend.cost_per_answer(ledger.path.parent)
    try:
        meter = spend.MeteredLLM(llm, judge.model.pricing, ledger.check(step, estimate))
    except spend.SpendCapError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except ValueError as exc:  # no pricing, or a ledger written under another cap
        console.print(f"[red]{exc}: nothing was judged.[/red]")
        raise typer.Exit(code=2) from exc
    return _RoundSpend(step, ledger, meter)


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
    """Judge a run's stored answers (supported? responsive?) with the LLM judge, or their support
    alone with the claim judge when the config's judge.method is claims. Costs money. The claim
    judge's spend is the judge round's: checked against its ledger before the first call, stopped
    at its cap, and recorded in it. An online run is judged only by a pinned judge: its grader
    name has a pin (online.ONLINE_JUDGE_PINS) and its version is that pin."""
    from concurrent.futures import ThreadPoolExecutor

    from rich.progress import Progress

    from agent_evals.graders.claim_judge import ClaimJudgeGrader
    from agent_evals.graders.judge import JudgeGrader
    from agent_evals.harness.cache import CallCache
    from agent_evals.harness.llm import AnthropicClient
    from agent_evals.harness.runner import grade_all
    from agent_evals.online import pin_refusal
    from agent_evals.spend import SpendCapError
    from agent_evals.store.db import ResultsStore, is_write_once
    from agent_evals.suites import load_suite
    from agent_evals.types import AgentResult

    load_dotenv()
    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        info = store.run_info(run_id)
        name = cfg.judge.grader_name  # its notes go in the manifest under this name
        held = info["manifest"].get(name)
        is_judge_note = isinstance(held, dict) and {"version", "rubric"} <= held.keys()
        if is_write_once(name) or (held is not None and not is_judge_note):
            console.print(
                f"[red]The judge's name {name!r} is the name of another record of {run_id}, "
                "which its notes would overwrite. Nothing was judged.[/red]"
            )
            raise typer.Exit(code=2)
        llm = AnthropicClient(CallCache(cfg.paths.cache_dir))
        grader: JudgeGrader | ClaimJudgeGrader = (
            ClaimJudgeGrader(llm, cfg.judge, salt=salt)  # support only: no responsive call
            if cfg.judge.method == "claims"
            else JudgeGrader(llm, cfg.judge, salt=salt)
        )
        if info["online"] and (refusal := pin_refusal(name, grader.version)):
            console.print(f"[red]{refusal}.[/red]")
            raise typer.Exit(code=2)
        suite_path = Path(str(info["manifest"].get("suite", {}).get("path", "")))
        if not suite_path.exists():
            console.print(f"[red]The run's suite file {suite_path} is gone; cannot judge.[/red]")
            raise typer.Exit(code=2)
        _require_unchanged_cases(info, suite_path)
        cases = {c.id: c for c in load_suite(suite_path)}
        todo = [a for a in store.answered_attempts(run_id) if a["case_id"] in cases]
        keys: list[tuple[str, int]] | None = None  # the grades replaced; None: all of the judge's
        if only_labeled is not None:
            labeled = {
                (x["case_id"], x["attempt"])
                for x in store.labels(run_id)
                if x["label_split"] == only_labeled
            }
            todo = [a for a in todo if (a["case_id"], a["attempt"]) in labeled]
            keys = [(a["case_id"], a["attempt"]) for a in todo]
        elif store.grade_count(run_id, cfg.judge.grader_name) and not replace:
            console.print(
                f"[red]This run already has {cfg.judge.grader_name} grades; pass --replace.[/red]"
            )
            raise typer.Exit(code=2)
        todo = todo[:limit]
        # Checked before any grade is deleted: a refusal leaves the run as it was.
        round_spend: _RoundSpend | None = None
        if isinstance(grader, ClaimJudgeGrader):
            round_spend = _round_spend(f"evals judge {run_id}", len(todo), llm, cfg.judge)
            grader = ClaimJudgeGrader(round_spend.meter, cfg.judge, salt=salt)
        store.delete_grades(run_id, cfg.judge.grader_name, keys)

        def one(a: dict[str, object]) -> tuple[dict[str, object], list[tuple[str, Grade]]]:
            case = cases[str(a["case_id"])]
            result = AgentResult(output=a["output"])  # type: ignore[arg-type]
            graded = grade_all([grader], case, result)
            stopped = round_spend.meter.stopped if round_spend is not None else None
            if stopped is not None:  # the cap stopped the step, not this answer: store nothing
                raise SpendCapError(stopped)
            return a, graded

        judged = 0
        done = False
        try:
            with ThreadPoolExecutor(max_workers=cfg.run.workers) as pool, Progress() as progress:
                task = progress.add_task("Judging", total=len(todo))
                for a, graded in pool.map(one, todo):
                    store.add_grades(run_id, str(a["case_id"]), int(str(a["attempt"])), graded)
                    judged += 1
                    progress.advance(task)
            done = True
        except SpendCapError as exc:
            console.print(
                f"[red]{exc} {judged} of the {len(todo)} answers were judged before the stop and "
                f"keep their grades; the rest have none from {cfg.judge.grader_name}, and the "
                "run's judge note is not written.[/red]"
            )
            raise typer.Exit(code=2) from exc
        finally:
            if round_spend is not None:
                round_spend.record(done=done)
        note: dict[str, Any] = {
            "version": grader.version,
            "model": cfg.judge.model.name,
            "rubric": cfg.judge.rubric_version,
            "salt": salt,
        }
        if isinstance(grader, ClaimJudgeGrader):  # its thresholds, and the salts of its retries
            note |= grader.run_note()
        store.annotate_run(run_id, cfg.judge.grader_name, note)
        u = grader.usage
        spent = ""  # the claim judge's spend is metered, and printed above as recorded
        if round_spend is None:
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

    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.text import Text

    from agent_evals import monitor as mon
    from agent_evals.corpus import Corpus
    from agent_evals.graders.judge_prompts import SUPPORT_RUBRICS
    from agent_evals.judge_validation import disagreements, label_split, revisit_targets
    from agent_evals.review import quote_context
    from agent_evals.store.db import ResultsStore
    from agent_evals.suites import load_suite

    if not _interactive():
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
        if show_judge and mon.blind_cases(store, run_id):
            console.print(
                f"[red]{run_id} holds answers of the online judge check, which are labeled "
                "blind until it is scored: --show-judge is refused on it until then.[/red]"
            )
            raise typer.Exit(code=2)
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
    from agent_evals import monitor as mon
    from agent_evals.judge_validation import agreement
    from agent_evals.store.db import ResultsStore

    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        if mon.blind_cases(store, run_id):  # its counts would tell the labeler the judge's verdicts
            console.print(
                f"[red]{run_id} holds answers of the online judge check, which are labeled blind "
                "until it is scored: judge-validate is refused on it until then.[/red]"
            )
            raise typer.Exit(code=2)
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
    """Mutation tests and bias probes for the judge, on answers it passed. Costs money. With the
    claim judge (judge.method claims), on the answers it passed on judge_supported, scored on
    judge_supported alone, and its spend is the judge round's: checked against its ledger before
    the first call, stopped at its cap, and recorded in it."""
    import json
    from concurrent.futures import ThreadPoolExecutor

    from rich.progress import Progress

    from agent_evals.graders.claim_judge import ClaimJudgeGrader
    from agent_evals.graders.judge import JudgeGrader
    from agent_evals.harness.cache import CallCache
    from agent_evals.harness.llm import AnthropicClient
    from agent_evals.harness.runner import grade_all
    from agent_evals.judge_probes import MUTANTS, Variant, behaved, report_path, variants
    from agent_evals.spend import SpendCapError
    from agent_evals.stats.core import wilson_ci
    from agent_evals.store.db import ResultsStore
    from agent_evals.suites import load_suite
    from agent_evals.types import AgentResult

    load_dotenv()
    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        from agent_evals import monitor as mon

        if mon.blind_cases(store, run_id):  # its misses would list the judge's verdicts
            console.print(
                f"[red]{run_id} holds answers of the online judge check, which are labeled "
                "blind until it is scored: judge-probe is refused on it until then.[/red]"
            )
            raise typer.Exit(code=2)
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
    claims = cfg.judge.method == "claims"
    # The verdicts a probe reads. The claim judge gives no responsive verdict, and its support
    # score and a judge error are no verdict (docs/plans/judge-v5.md).
    scored = ("judge_supported",) if claims else ("judge_supported", "judge_responsive")
    passed_both = [
        a
        for a in answered
        if a["case_id"] in cases
        and all(verdicts.get((a["case_id"], a["attempt"]), {}).get(m) == 1.0 for m in scored)
    ][:limit]
    pool = [
        (str(c.get("url", "")), str(c.get("quote", "")))
        for a in answered
        for c in (a["output"].get("citations") or [])
        if isinstance(c, dict)
    ]
    llm = AnthropicClient(CallCache(cfg.paths.cache_dir))
    jobs = [(a, v) for a in passed_both for v in variants(a["output"], pool)]
    round_spend = (
        _round_spend(f"evals judge-probe {run_id}", len(jobs), llm, cfg.judge) if claims else None
    )
    client: LLMClient = llm if round_spend is None else round_spend.meter

    def grader(salt: str) -> JudgeGrader | ClaimJudgeGrader:
        if claims:
            return ClaimJudgeGrader(client, cfg.judge, salt=salt)
        return JudgeGrader(client, cfg.judge, salt=salt)

    graders = {salt: grader(salt) for salt in ("judge", "probe")}

    def one(job: tuple[dict[str, Any], Variant]) -> tuple[dict[str, Any], Variant, bool, str]:
        a, v = job
        graded = grade_all([graders[v.salt]], cases[a["case_id"]], AgentResult(output=v.output))
        stopped = round_spend.meter.stopped if round_spend is not None else None
        if stopped is not None:  # the cap stopped the step, not this variant: no result for it
            raise SpendCapError(stopped)
        judged = {g.metric: g.value for _, g in graded if g.metric != "grader_error"}
        original = verdicts[(a["case_id"], a["attempt"])]
        if claims:
            judged = {m: x for m, x in judged.items() if m in scored}
            original = {m: x for m, x in original.items() if m in scored}
        ok = behaved(v, original, judged)
        reasons = "; ".join(f"{g.metric}={g.value:g} {g.reason}" for _, g in graded)
        return a, v, ok, reasons

    tally: dict[str, list[bool]] = {}
    misses: list[dict[str, Any]] = []
    done = False
    try:
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
        done = True
    except SpendCapError as exc:
        console.print(f"[red]{exc} No probe result is reported.[/red]")
        raise typer.Exit(code=2) from exc
    finally:
        if round_spend is not None:
            round_spend.record(done=done)

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
    from agent_evals import monitor as mon
    from agent_evals.report import compare
    from agent_evals.store.db import ResultsStore

    cfg = _config(config)
    store = ResultsStore(cfg.paths.db)
    try:
        if metric.startswith("judge_") and any(
            mon.blind_cases(store, run) for run in (baseline, candidate)
        ):  # its newly passing and failing lists would show the judge's verdicts
            console.print(
                "[red]These runs hold answers of the online judge check, which are labeled "
                "blind until it is scored: comparing them on a judge metric is refused until "
                "then.[/red]"
            )
            raise typer.Exit(code=2)
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
    halves: Annotated[
        int,
        typer.Option(
            "--halves",
            min=1,
            help="Halves of 100 questions, each planned from its own seed; part 5's 200-question "
            "days take 2. The duplicate check runs across halves.",
        ),
    ] = 1,
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing day whose questions differ.")
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Generate one simulated day of online traffic (phase 7): a generator call per question, a
    checker call per lookup and comparison, and any retries. Costs about $0.65 per 100
    questions."""
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
    slots = [
        s for h in range(1, halves + 1) for s in plan_day(day, corpus, excluded, services, half=h)
    ]
    llm = AnthropicClient(CallCache(cfg.paths.cache_dir))
    questions, redo = generate_day(llm, slots, corpus, areas, model, pool)
    traffic = "".join(q.model_dump_json() + "\n" for q in questions)
    meta = day_summary(questions) | {
        "day": day,
        "halves": halves,
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
    """Whether a person at a terminal types into stdin. On Windows the NUL device, which a shell
    with no terminal often hands a child process, also reports isatty(), so a console is
    required there too: otherwise a screen meant for a person runs until its first prompt reads
    the end of input."""
    import sys

    if not sys.stdin.isatty():
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    except (OSError, ValueError):
        return False
    mode = ctypes.c_ulong()
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def _print_monitor(report: dict[str, Any], title: str) -> None:
    """One day's monitor table: every metric with its change, p, floor and blind spot. Rows are
    labelled by what they count (judge_supported counts answers not fully supported), and
    latency and tokens are shown as geometric means, their changes as ratios."""
    from math import exp

    from agent_evals import monitor as mon

    labels = {s.name: mon.shown_label(s) for s in mon.ALERT_FAMILY}
    table = Table(show_header=True, header_style="bold", title=title)
    for col in ("metric", "day", "reference", "change", "p", "floor", "detectable", "status"):
        table.add_column(col, justify="left" if col in ("metric", "status") else "right")

    def fmt(row: dict[str, Any], key: str) -> str:
        value = row.get(key)
        if value is None:  # a blind spot with no number means no day of this n can alert
            return "cannot alert" if key == "detectable" and row.get("detectable_note") else "-"
        if key == "p":
            return f"{value:.4f}"
        if row["metric"] in ("latency", "tokens") and key in ("effect", "floor", "detectable"):
            return f"{(exp(value) - 1) * 100:+.0f}%"  # a log difference, shown as a ratio
        if row["metric"] == "latency":
            return f"{exp(value) / 1000:.1f} s"
        if row["metric"] == "tokens":
            return f"{exp(value) / 1000:.1f}k"
        if row["kind"] == "rate" and key in ("day", "ref"):
            return f"{value:.3f}"
        if row["kind"] == "rate":
            return f"{value * 100:+.1f} pts"
        return f"{value:+.2f}" if key in ("effect", "floor", "detectable") else f"{value:.2f}"

    for row in report["metrics"]:
        colour = "red" if row["status"] == "alert" else ""
        status = f"[{colour}]{row['status']}[/{colour}]" if colour else row["status"]
        table.add_row(
            labels.get(row["metric"], row["metric"]),  # no [ ] in a label: no markup
            fmt(row, "day"),
            fmt(row, "ref"),
            fmt(row, "effect"),
            fmt(row, "p"),
            fmt(row, "floor"),
            fmt(row, "detectable"),
            status,
        )
    console.print(table)
    fab = report["fabricated_citation"]
    if fab["p"] is not None:
        verdict = "[red]alert[/red]" if fab["alert"] else "ok"
        blind = fab.get("detectable")
        seen = (
            f", detectable +{blind * 100:.1f} pts"
            if blind is not None
            else (", cannot alert at this n" if fab.get("detectable_note") else "")
        )
        console.print(
            f"fabricated citations, own rule (p < 0.01): day {fab['day']:.3f}, reference "
            f"{fab['ref']:.3f}, p {fab['p']:.4f}{seen}: {verdict}"
        )
    judge = report["judge"]
    trust = "trusted" if judge["trusted"] else "[yellow]untrusted today[/yellow]"
    standing = judge.get("standing") or ("in the family" if judge["in_family"] else "pending")
    errors = judge.get("errors") or 0
    erred = (  # answers with no verdict, kept out of judge_supported
        f"; {errors} judge error(s) in {judge.get('cited')} cited answers (untrusted past "
        f"{mon.JUDGE_ERROR_SHARE:.0%})"
        if errors
        else ""
    )
    console.print(f"judge: {standing}; {trust}{erred}")
    console.print(f"reported only: {json.dumps(report['reported'])}")


# What a recorded day's report must hold to be shown and to give its exit code.
MONITOR_REPORT_KEYS = (
    "run_id",
    "metrics",
    "fabricated_citation",
    "judge",
    "reported",
    "alerts",
    "untested",
)


def _recorded_verdicts() -> dict[str, Any]:
    """Each checked day's recorded verdict, from alerts.json: day -> the monitor's report."""
    from agent_evals import monitor as mon

    if not mon.ALERTS_PATH.exists():
        return {}
    return dict(json.loads(mon.ALERTS_PATH.read_text(encoding="utf-8")))


def _verdict_line(day: int, report: dict[str, Any]) -> int:
    """Print a day's verdict; return the monitor's exit code for it (monitor.verdict_code)."""
    from agent_evals import monitor as mon

    code = mon.verdict_code(report)
    if code == 1:
        console.print(f"[red]ALERT on day {day}: {', '.join(report['alerts'])}[/red]")
    elif code == 3:
        reasons = "; ".join(mon.not_cleared(report))
        console.print(f"[yellow]Day {day}: no alert, but not cleared: {reasons}.[/yellow]")
    else:
        console.print(f"[green]Day {day}: no alert.[/green]")
    return code


def _recorded_day(day: int, recorded: Any, run_id: str | None) -> int:
    """A day whose verdict is recorded: the record printed and its exit code returned, with
    nothing recomputed or written, so asking again can never change a pre-registered verdict."""
    from agent_evals import monitor as mon

    if not isinstance(recorded, dict) or not all(k in recorded for k in MONITOR_REPORT_KEYS):
        console.print(
            f"[red]Day {day}'s record in {mon.ALERTS_PATH} is not a monitor report, so its "
            "verdict cannot be shown. It stands all the same: the day is not checked again.[/red]"
        )
        return 2
    run = recorded["run_id"]
    _print_monitor(recorded, f"day {day} ({run}) against the reference days, as recorded")
    console.print(
        f"Day {day}'s verdict was recorded at {recorded.get('checked_at', '?')} "
        f"({mon.ALERTS_PATH}) and stands: it is shown, not checked again. To read the day again "
        f"after it: uv run evals monitor {day} --post-hoc"
    )
    if run_id is not None and run_id != run:
        console.print(
            f"[yellow]The recorded verdict is {run}'s, not {run_id}'s: a day has one verdict."
            "[/yellow]"
        )
    return _verdict_line(day, recorded)


@app.command()
def monitor(
    day: Annotated[
        int | None, typer.Argument(help="Day to check against the frozen reference days.")
    ] = None,
    freeze: Annotated[
        bool,
        typer.Option(
            "--freeze",
            help="Check days 1 to 5 against each other (leave one day out) and, if no alert, "
            "freeze them as the reference.",
        ),
    ] = False,
    refreeze: Annotated[
        bool,
        typer.Option(
            "--refreeze",
            help="With --freeze: check again although a reference is frozen, or a check already "
            "stopped the demo. Everything from before (the reference, its check, alerts, "
            "post-hoc readings, the judge check) is first moved to results/online/archive/, "
            "never deleted.",
        ),
    ] = False,
    run_id: Annotated[
        str | None,
        typer.Option("--run", help="The online run to check; needed when the day has several."),
    ] = None,
    post_hoc: Annotated[
        bool,
        typer.Option(
            "--post-hoc",
            help="Read a day whose verdict is recorded again, from today's stored grades, into "
            "results/online/post-hoc.json: a post-hoc reading, never a verdict, and never "
            "written to alerts.json. It reads the recorded run unless --run names another.",
        ),
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Is a day worse than the reference days (phase 7 part 3)? Reads stored runs only, so it is
    free. Exits 0 when the day is clear, 1 on an alert (or a reference too noisy to freeze), 3
    when there is no alert but the day is not cleared (a family metric untested, or the judge
    untrusted), and 2 when the day cannot be checked. A day's verdict is recorded once, in
    alerts.json, so no pre-registered verdict can be overwritten: asked again, the monitor
    prints the recorded verdict and exits with its code, recomputing and writing nothing.
    --post-hoc reads a recorded day again and writes only to post-hoc.json; it exits 0 once the
    reading is written, whatever it found, since a post-hoc reading is never a verdict."""
    from datetime import UTC, datetime

    from agent_evals import monitor as mon
    from agent_evals.store.db import ResultsStore

    cfg = _config(config)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if post_hoc and (freeze or day is None):
        console.print(
            "[red]--post-hoc reads one day whose verdict is recorded: give that day, without "
            "--freeze.[/red]"
        )
        raise typer.Exit(code=2)
    recorded: Any = None  # the day's recorded verdict, read before anything else
    if not freeze and day is not None and day not in mon.REFERENCE_DAYS:
        recorded = _recorded_verdicts().get(str(day))
        if recorded is not None and not post_hoc:
            raise typer.Exit(code=_recorded_day(day, recorded, run_id))
        if recorded is None and post_hoc:
            console.print(
                f"[red]Day {day} has no recorded verdict: uv run evals monitor {day} records "
                "it. A post-hoc reading comes after the verdict, never instead of it.[/red]"
            )
            raise typer.Exit(code=2)
        if post_hoc and run_id is None and isinstance(recorded, dict) and recorded.get("run_id"):
            run_id = str(recorded["run_id"])  # the run the verdict was given on, unless named
    try:
        store = ResultsStore(cfg.paths.db, read_only=True)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None
    try:
        if freeze:
            check_path = mon.REFERENCE_PATH.with_name("reference-check.json")
            if not refreeze and (mon.REFERENCE_PATH.exists() or check_path.exists()):
                why = (
                    f"The reference is already frozen ({mon.REFERENCE_PATH})"
                    if mon.REFERENCE_PATH.exists()
                    else f"The reference check already stopped the demo ({check_path})"
                )
                console.print(f"[red]{why}. Pass --refreeze to check again.[/red]")
                raise typer.Exit(code=2)
            try:
                found = {d: mon.day_run(store, d, champion_only=True) for d in mon.REFERENCE_DAYS}
            except ValueError as exc:
                console.print(f"[red]{exc}. Which run counts is a decision: not frozen.[/red]")
                raise typer.Exit(code=2) from None
            if missing := [d for d, r in found.items() if r is None]:
                console.print(
                    f"[red]No complete champion online run for day(s) {missing}: run "
                    "`uv run evals online-run N` first.[/red]"
                )
                raise typer.Exit(code=2)
            runs = {d: str(r) for d, r in found.items()}
            try:
                days = [mon.load_day(store, runs[d], TRAFFIC_DIR) for d in mon.REFERENCE_DAYS]
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=2) from None
            # The judge is never in the family here: it joins through `judge-check score`, which
            # reruns this check with judge_supported in the family.
            checks = mon.leave_one_day_out(days, judge_enabled=False)
            for d, report in zip(mon.REFERENCE_DAYS, checks, strict=True):
                _print_monitor(report, f"day {d} against the other reference days")
            record: dict[str, Any] = {
                "frozen_at": now,
                "checked_at": now,
                "days": {str(d): runs[d] for d in mon.REFERENCE_DAYS},
                "judge_in_family": False,
                "leave_one_day_out": checks,
            }
            previous = (
                json.loads(mon.REFERENCE_PATH.read_text(encoding="utf-8"))
                if mon.REFERENCE_PATH.exists()
                else None
            )
            same_runs = previous is not None and previous.get("days") == record["days"]
            if previous is not None and same_runs:
                # The same runs: the reference stays frozen from when it first was, and this
                # check only re-records it with today's code.
                record["frozen_at"] = previous.get("frozen_at") or previous.get("checked_at", now)
            if refreeze:  # what was checked before goes to the archive, whatever this finds
                stamp = now.replace(":", "").replace("+0000", "Z")
                moved = mon.archive_reference_files(stamp, keep_judge_check=same_runs)
                for path in moved:
                    console.print(f"archived {path}")
                kept = [p for p in (mon.JUDGE_SAMPLE_PATH, mon.JUDGE_CHECK_PATH) if p.exists()]
                if same_runs and kept:
                    console.print(
                        f"kept {', '.join(p.name for p in kept)}: they belong to the same runs"
                    )
                record["previous_record"] = next(
                    (p.as_posix() for p in moved if p.name == mon.REFERENCE_PATH.name), None
                )
            if noisy := mon.noisy_days(runs, checks):
                check_path.parent.mkdir(parents=True, exist_ok=True)
                check_path.write_text(
                    json.dumps(record, indent=2, allow_nan=False) + "\n", encoding="utf-8"
                )
                console.print(
                    f"[red]The reference is too noisy: {noisy}. By the plan the demo stops here; "
                    f"details in {check_path}.[/red]"
                )
                raise typer.Exit(code=1)
            mon.REFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
            mon.REFERENCE_PATH.write_text(
                json.dumps(record, indent=2, allow_nan=False) + "\n", encoding="utf-8"
            )
            console.print(
                f"[green]No reference day alerts against the others: days 1 to 5 frozen as the "
                f"reference ({mon.REFERENCE_PATH}).[/green]"
            )
            return
        if day is None:
            console.print("[red]Give a day to check, or --freeze to set up the reference.[/red]")
            raise typer.Exit(code=2)
        if day in mon.REFERENCE_DAYS:
            console.print(
                f"[red]Day {day} is a reference day: `--freeze` checks days 1 to 5 against "
                "each other.[/red]"
            )
            raise typer.Exit(code=2)
        if not mon.REFERENCE_PATH.exists():
            console.print(
                "[red]No frozen reference yet: run `uv run evals monitor --freeze`.[/red]"
            )
            raise typer.Exit(code=2)
        reference = json.loads(mon.REFERENCE_PATH.read_text(encoding="utf-8"))
        ref_runs = [str(r) for r in reference["days"].values()]
        if run_id is not None:
            try:
                role = store.run_info(run_id)["manifest"].get("online") or {}
            except KeyError as exc:
                console.print(f"[red]{exc.args[0]}[/red]")
                raise typer.Exit(code=2) from None
            if role.get("day") != day:
                console.print(f"[red]{run_id} is a run of day {role.get('day')}, not {day}.[/red]")
                raise typer.Exit(code=2)
            if role.get("shadow"):
                console.print(
                    f"[red]{run_id} is a shadow run: the monitor checks the run that served "
                    "the day.[/red]"
                )
                raise typer.Exit(code=2)
            target: str | None = run_id
        else:
            try:
                target = mon.day_run(store, day)
            except ValueError as exc:
                hint = (
                    f" (uv run evals blind match {day} names the one under its sealed choice)"
                    if day in BLIND_DAYS
                    else ""
                )
                console.print(f"[red]{exc}: pass --run to choose{hint}.[/red]")
                raise typer.Exit(code=2) from None
        if target is None:
            console.print(f"[red]No complete online run for day {day}.[/red]")
            raise typer.Exit(code=2)
        enabled, standing = mon.judge_standing(store, ref_runs)
        try:
            report = mon.check_day(
                mon.load_day(store, target, TRAFFIC_DIR),
                [mon.load_day(store, r, TRAFFIC_DIR) for r in ref_runs],
                judge_enabled=enabled,
                judge_note=None if enabled else standing,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from None
    finally:
        store.close()
    if post_hoc:  # to its own file, never alerts.json, and never read as a verdict
        readings = (
            json.loads(mon.POST_HOC_PATH.read_text(encoding="utf-8"))
            if mon.POST_HOC_PATH.exists()
            else {}
        )
        source = recorded if isinstance(recorded, dict) else {}
        verdict = {k: source.get(k) for k in ("checked_at", "run_id", "alerts")}
        reading = {"checked_at": now, "post_hoc": True, "recorded_verdict": verdict, **report}
        readings.setdefault(str(day), []).append(reading)  # every reading kept, in order
        mon.POST_HOC_PATH.parent.mkdir(parents=True, exist_ok=True)
        mon.POST_HOC_PATH.write_text(
            json.dumps(readings, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        _print_monitor(report, f"day {day} ({target}) against the reference days, post hoc")
        now_shows = f"alerts on {', '.join(report['alerts'])}" if report["alerts"] else "no alert"
        recorded_alerts = ", ".join(verdict["alerts"] or []) or "no alert"
        console.print(
            f"[yellow]Post hoc, not a verdict: read again now, day {day} shows {now_shows}. Its "
            f"recorded verdict ({verdict['checked_at']}) stands: {recorded_alerts}. This "
            f"reading is written to {mon.POST_HOC_PATH} only.[/yellow]"
        )
        return
    alerts = _recorded_verdicts()  # read again: another check may have recorded the day since
    if str(day) in alerts:
        console.print(
            f"[red]Day {day}'s verdict was recorded while this check ran "
            f"({alerts[str(day)].get('checked_at', '?')}). It stands; this check is not written."
            "[/red]"
        )
        raise typer.Exit(code=2)
    _print_monitor(report, f"day {day} ({target}) against the reference days")
    alerts[str(day)] = {"checked_at": now, **report}
    mon.ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    mon.ALERTS_PATH.write_text(
        json.dumps(alerts, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    if code := _verdict_line(day, report):
        raise typer.Exit(code=code)


@app.command()
def dashboard(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Build the online dashboard (phase 7 part 4): one static page, docs/dashboard/index.html,
    from the stored runs and the monitor's files. Free."""
    from agent_evals import dashboard as dash
    from agent_evals import monitor as mon
    from agent_evals.simulate import load_traffic
    from agent_evals.store.db import ResultsStore

    cfg = _config(config)
    reference = (
        json.loads(mon.REFERENCE_PATH.read_text(encoding="utf-8"))
        if mon.REFERENCE_PATH.exists()
        else None
    )
    checked = (
        json.loads(mon.ALERTS_PATH.read_text(encoding="utf-8")) if mon.ALERTS_PATH.exists() else {}
    )
    try:
        store = ResultsStore(cfg.paths.db, read_only=True)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None
    points: list[dash.DayPoint] = []
    try:
        if reference is not None:
            ref_runs = {int(d): str(r) for d, r in reference["days"].items()}
        else:  # not frozen yet: show whichever reference days have a checkable run
            ref_runs = {}
            for d in mon.REFERENCE_DAYS:
                try:
                    found = mon.day_run(store, d, champion_only=True)
                    if found is not None:
                        mon.load_day(store, found, TRAFFIC_DIR)
                        ref_runs[d] = found
                except ValueError as exc:
                    console.print(f"[yellow]day {d} left out: {exc}[/yellow]")
        # every judge's check sample: never shown with a verdict, the validated judge's or not
        blind = mon.sampled_cases(store, ref_runs.values()) if ref_runs else {}
        judge_in, standing = (
            mon.judge_standing(store, ref_runs.values()) if reference is not None else (False, None)
        )
        days = [(d, r, True, None) for d, r in sorted(ref_runs.items())]
        for key, rep in sorted(checked.items(), key=lambda kv: int(kv[0])):
            run = str(rep["run_id"])
            if sorted(rep.get("reference_runs") or []) != sorted(ref_runs.values()):
                console.print(
                    f"[yellow]day {key} left out: checked against another reference[/yellow]"
                )
                continue
            if (store.run_info(run)["manifest"].get("online") or {}).get("day") != int(key):
                console.print(f"[yellow]day {key} left out: {run} is a run of another day[/yellow]")
                continue
            days.append((int(key), run, False, rep))
        for d, run, is_ref, report in days:
            traffic = {q.id: q for q in load_traffic(TRAFFIC_DIR / f"day-{d:02d}.jsonl")}
            points.append(
                dash.DayPoint(
                    day=d,
                    run_id=run,
                    reference=is_ref,
                    values=mon.load_day(store, run, TRAFFIC_DIR),
                    report=report,
                    worst=dash.least_supported(store, run, traffic, hidden=blind.get(run, ())),
                )
            )
    except (KeyError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None
    finally:
        store.close()
    html = dash.render(points, reference, judge_in, standing)
    dash.DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    dash.DASHBOARD_PATH.write_text(html, encoding="utf-8")
    console.print(f"wrote {dash.DASHBOARD_PATH} ({len(points)} days)")


@app.command("judge-check")
def judge_check(
    action: Annotated[
        str,
        typer.Argument(
            help="label: label the 30 sampled answers, blind; score: count the judge's false passes"
        ),
    ],
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """May judge_supported alert (phase 7 part 3)? The author labels 30 judged answers from the
    reference days, blind: 3 the judge passed and 3 it failed from each day, in an order that
    shows neither. The judge may pass at most 2 of the ones the author fails. If so, the
    leave-one-day-out check runs again with judge_supported in the family and must stay quiet.
    `label` draws the sample the first time and can be stopped with q and run again to
    continue. `score` records the result for the monitor, and its first complete result is
    final: the result shows the judge's verdicts, so a second score after relabeling would no
    longer be blind. The sample and the result are kept in the results database; the two files
    in results/online/ are only their published copies, restored from it if they go astray."""
    from datetime import UTC, datetime

    from agent_evals import monitor as mon
    from agent_evals.store.db import ResultsStore

    cfg = _config(config)
    labeler = mon.JUDGE_CHECK_LABELER
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if action not in ("label", "score"):
        console.print("[red]The action is label or score.[/red]")
        raise typer.Exit(code=2)
    if not mon.REFERENCE_PATH.exists():
        console.print("[red]Freeze the reference first: uv run evals monitor --freeze[/red]")
        raise typer.Exit(code=2)
    runs = {
        int(d): str(r)
        for d, r in json.loads(mon.REFERENCE_PATH.read_text(encoding="utf-8"))["days"].items()
    }

    def publish(path: Path, record: dict[str, Any]) -> bool:
        """Write the published copy of a database record; True if it was missing or differed."""
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if current == record:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return True

    try:
        store = ResultsStore(cfg.paths.db, read_only=True)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None
    new_draw = False
    try:
        if sorted(runs) != list(mon.REFERENCE_DAYS):
            console.print(f"[red]{mon.REFERENCE_PATH} does not name days 1 to 5.[/red]")
            raise typer.Exit(code=2)
        for key in (mon.JUDGE_SAMPLE_RECORD, mon.JUDGE_CHECK_RECORD):  # records are written once
            if (taken := mon.foreign_record(store, runs.values(), key)) is not None:
                console.print(
                    f"[red]{taken} already belongs to the judge check of another reference "
                    "(its sample and score stay with it), so it cannot join this one.[/red]"
                )
                raise typer.Exit(code=2)
        drawn = mon.recorded_sample(store, runs.values())
        first = mon.recorded_judge_check(store, runs.values())
        for d, run in sorted(runs.items()):  # the frozen champion runs, never a hand-edited list
            if drawn is None:  # the draw: each day's one complete champion run
                try:
                    ok = mon.day_run(store, d, champion_only=True) == run
                except ValueError:
                    ok = False
            else:  # drawn: the runs it was drawn on, each still a complete champion run
                ok = run in mon.day_runs(store, d, champion_only=True)
            if not ok:
                console.print(
                    f"[red]{mon.REFERENCE_PATH} names {run} for day {d}, which is not that day's "
                    "complete champion online run the judge check belongs to. It runs only on "
                    "the frozen reference.[/red]"
                )
                raise typer.Exit(code=2)
        stamp = now.replace(":", "").replace("+0000", "Z")
        for path, record in ((mon.JUDGE_SAMPLE_PATH, drawn), (mon.JUDGE_CHECK_PATH, first)):
            if record is not None:  # the published copy always matches the database
                if publish(path, record):
                    console.print(f"{path} was restored from the results database.")
            elif path.exists():  # a copy of nothing: it counts for nothing, so move it aside
                dest = mon.REFERENCE_PATH.parent / "archive" / stamp / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                path.replace(dest)
                console.print(
                    f"[yellow]{path} matched no record in the results database, so it counted "
                    f"for nothing; moved to {dest}.[/yellow]"
                )
        if action == "score" and first is not None:  # the first complete score is final
            verdict = "passed" if first.get("passed") else "did not pass"
            console.print(
                f"The judge check was scored at {first.get('scored_at', '?')} and {verdict}. "
                f"Its first complete result is final ({mon.JUDGE_CHECK_PATH})."
            )
            return
        if drawn is None:
            if action == "score":  # only `label` draws the sample
                console.print(
                    "[red]No judge-check sample yet: `uv run evals judge-check label` draws it."
                    "[/red]"
                )
                raise typer.Exit(code=2)
            if any(store.labels(run) for run in runs.values()):  # anyone's, under any name
                console.print(
                    "[red]The reference runs already have labels, so a sample drawn now would "
                    "not be blind to them. Nothing was drawn.[/red]"
                )
                raise typer.Exit(code=2)
            from agent_evals import dashboard as dash

            # the dashboard has published these with the judge's verdicts: never sample them
            shown = dash.shown_with_verdicts(store, runs.values())
            try:
                sample = mon.judge_check_sample(store, runs, shown)
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=2) from None
            drawn = {
                "drawn_at": now,
                "reference_runs": sorted(runs.values()),
                "runs": sample,
                "verdict_digests": mon.verdict_digests(store, sample),
            }
            new_draw = True
        missing = [  # a draw cut short by a crash: its record is copied to the rest
            run
            for run in runs.values()
            if mon.JUDGE_SAMPLE_RECORD not in store.run_info(run)["manifest"]
        ]
        sample = {str(r): list(c) for r, c in drawn["runs"].items()}
        if mon.verdict_digests(store, sample) != drawn.get("verdict_digests"):
            console.print(
                "[red]The judge's grades on the sampled answers have changed since the sample "
                f"was drawn ({drawn.get('drawn_at', '?')}), so this would no longer be the "
                "check that was drawn. Nothing was labeled or scored.[/red]"
            )
            raise typer.Exit(code=2)
        if action == "score":
            try:  # the days first, whatever the labels say: a refusal must not hint at them
                days = [mon.load_day(store, runs[d], TRAFFIC_DIR) for d in mon.REFERENCE_DAYS]
                result = mon.judge_check_score(store, sample, labeler)
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=2) from None
    finally:
        store.close()
    if missing:  # first in the database, which no file can outlive or overrule; all or none
        writer = ResultsStore(cfg.paths.db)
        try:
            writer.annotate_runs(missing, mon.JUDGE_SAMPLE_RECORD, drawn)
        finally:
            writer.close()
        if not new_draw:
            console.print(f"the sample's record was copied to {', '.join(missing)}")
    publish(mon.JUDGE_SAMPLE_PATH, drawn)
    total = sum(len(cases) for cases in sample.values())
    if action == "label":
        for n, (run, cases) in enumerate(sample.items(), 1):
            console.print(f"[bold]Reference day {n} of {len(sample)}[/bold] ({run})")
            label(
                run_id=run,
                labeler=labeler,
                limit=None,
                attempt=1,
                revisit=None,
                split=None,
                show_judge=False,
                case_ids=list(cases),
                fresh=False,
                config=config,
            )
            store = ResultsStore(cfg.paths.db, read_only=True)
            try:
                done_cases = {x["case_id"] for x in store.labels(run, labeler)}
            finally:
                store.close()
            if not set(cases) <= done_cases:  # stopped with q: stop here, resume next time
                console.print("Stopped. Run the same command to continue where you left off.")
                return
        console.print(f"[green]All {total} labeled. Next: uv run evals judge-check score[/green]")
        return
    if not result["complete"]:
        console.print(
            f"[yellow]{result['labeled']} of {result['answers']} labeled; finish with "
            "uv run evals judge-check label[/yellow]"
        )
        raise typer.Exit(code=1)
    noisy: dict[str, Any] = {}
    if result["labels_passed"]:  # the noise check again, now with judge_supported in the family
        checks = mon.leave_one_day_out(days, judge_enabled=True)
        noisy = mon.noisy_days(runs, checks)
        result["leave_one_day_out"] = [
            {
                "run_id": c["run_id"],
                "alerts": c["alerts"],
                "untested": c["untested"],
                "judge_supported": next(
                    m for m in c["metrics"] if m["metric"] == "judge_supported"
                ),
            }
            for c in checks
        ]
    result["noisy"] = noisy
    result["passed"] = result["labels_passed"] and not noisy
    result["scored_at"] = now
    writer = ResultsStore(cfg.paths.db)  # first in the database, which a file cannot outlive
    try:
        writer.annotate_runs(list(runs.values()), mon.JUDGE_CHECK_RECORD, result)  # all or none
    finally:
        writer.close()
    publish(mon.JUDGE_CHECK_PATH, result)
    console.print(
        f"It failed {result['false_fails']} answer(s) you passed, of the "
        f"{result['answers'] - result['judge_passed']} it failed (reported, no bar)."
    )
    counted = (
        f"The judge passed {result['false_passes']} answer(s) you failed, of the "
        f"{result['judge_passed']} it passed (at most {result['max_false_passes']} allowed)"
    )
    if not result["labels_passed"]:
        console.print(f"{counted}: it fails, so judge_supported stays out of the alert family.")
    elif noisy:
        console.print(
            f"{counted}: the labels pass. But with judge_supported in the family the reference "
            f"is too noisy ({noisy}), so judge_supported stays out of the alert family."
        )
    else:
        console.print(
            f"{counted}: it passes, and the reference stays quiet with judge_supported in the "
            "family: judge_supported may now alert."
        )


@app.command("online-run")
def online_run(
    day: Annotated[int, typer.Argument(help="Simulated day to run.")],
    judge_answers: Annotated[
        bool,
        typer.Option(
            "--judge/--no-judge", help="Judge the answers with the config's judge, if pinned."
        ),
    ] = True,
    shadow: Annotated[
        bool,
        typer.Option(
            "--shadow",
            help="A shadow run: the champion on the questions of a day another config serves "
            "(part 5). It is compared with the served run; the monitor never checks it.",
        ),
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Run the docs agent on one simulated day, grade what needs no reference answer, and judge it
    with the config's judge (phase 7 part 2), which must be pinned: its grader name has a pin and
    its version is that pin, or nothing runs. Costs money. The run is never the latest run. Any
    config may run (part 5's fault and shadow days need others); the run records which, whether
    it was the champion's (configs/default.yaml), whether it is a shadow, and the hash of the
    traffic file it ran. A blind day is served only by `evals blind run DAY`; here it takes
    --shadow, once the monitor has recorded the day's verdict."""
    from agent_evals import monitor as mon

    if day in BLIND_DAYS and not shadow:
        console.print(
            f"[red]Day {day} is a blind day: only uv run evals blind run {day} serves it, under "
            "its sealed choice. Nothing was run.[/red]"
        )
        raise typer.Exit(code=2)
    verdicts = (
        json.loads(mon.ALERTS_PATH.read_text(encoding="utf-8")) if mon.ALERTS_PATH.exists() else {}
    )
    if day in BLIND_DAYS and str(day) not in verdicts:  # its cache hits would hint at the config
        console.print(
            f"[red]Day {day}'s shadow runs after its verdict: uv run evals monitor {day} first. "
            "Nothing was run.[/red]"
        )
        raise typer.Exit(code=2)
    _online_run(day=day, judge_answers=judge_answers, shadow=shadow, config=config)


def _online_judge_refusal(judge: JudgeConfig) -> str | None:
    """Why this judge config may not grade an online run, or None when it may: its grader name
    is pinned and its judge's version is that pin (online.pin_refusal). Builds the judge the
    config names, the claim judge or the verdict judge, and calls nothing."""
    from agent_evals.graders.claim_judge import ClaimJudgeGrader
    from agent_evals.graders.judge import JudgeGrader
    from agent_evals.harness.llm import AnthropicClient
    from agent_evals.online import pin_refusal

    try:
        grader: JudgeGrader | ClaimJudgeGrader = (
            ClaimJudgeGrader(AnthropicClient(), judge)
            if judge.method == "claims"
            else JudgeGrader(AnthropicClient(), judge)
        )
    except ValueError as exc:  # a judge config no judge accepts grades nothing
        return f"The config's judge cannot be built: {exc}"
    return pin_refusal(judge.grader_name, grader.version)


def _online_run(*, day: int, judge_answers: bool, shadow: bool, config: Path) -> str:
    """online-run's work, which `evals blind run` calls too. Returns the run's id."""
    from agent_evals.harness.manifest import file_sha256
    from agent_evals.online import ONLINE_CASES_DIR, online_case, online_suite_name
    from agent_evals.simulate import load_traffic
    from agent_evals.store.db import ResultsStore
    from agent_evals.suites import write_suite

    cfg = _config(config)
    from agent_evals import monitor as mon

    frozen = (
        json.loads(mon.REFERENCE_PATH.read_text(encoding="utf-8")).get("days") or {}
        if mon.REFERENCE_PATH.exists()
        else {}
    )
    if str(day) in frozen:  # a second run would make the reference ambiguous
        console.print(
            f"[red]Day {day} is a frozen reference day ({frozen[str(day)]}): another run of it "
            "would make the reference, and the judge check drawn on it, ambiguous. Nothing was "
            "run.[/red]"
        )
        raise typer.Exit(code=2)
    if judge_answers and (refusal := _online_judge_refusal(cfg.judge)):  # before the agent runs
        console.print(f"[red]{refusal}. Nothing was run.[/red]")
        raise typer.Exit(code=2)
    traffic_path = TRAFFIC_DIR / f"day-{day:02d}.jsonl"
    if not traffic_path.exists():
        console.print(
            f"[red]No simulated traffic for day {day} at {traffic_path}.[/red] Generate it "
            f"first: uv run evals simulate {day}"
        )
        raise typer.Exit(code=2)
    traffic_sha256 = file_sha256(traffic_path)
    cases_path = ONLINE_CASES_DIR / f"{online_suite_name(day)}.jsonl"
    write_suite(cases_path, [online_case(q) for q in load_traffic(traffic_path)])
    run_id = run(
        suite=cases_path, agent="docs", attempts=1, limit=None, allow_drafts=False, config=config
    )
    store = ResultsStore(cfg.paths.db)
    try:
        champion = config.resolve() == DEFAULT_CONFIG.resolve()
        role = {
            "day": day,
            "config": config.as_posix(),
            "champion": champion,
            "shadow": shadow,
            "traffic_sha256": traffic_sha256,
        }
        store.annotate_run(run_id, "online", role)
    finally:
        store.close()
    if judge_answers:
        judge(
            run_id=run_id, limit=None, salt="judge", replace=False, only_labeled=None, config=config
        )
    return run_id


BLIND_DAYS = (9, 10, 11)  # part 5, item 5: the days the author's sealed choices decide
BLIND_CHOICES: dict[str, Path] = {  # what each choice runs
    "unchanged": DEFAULT_CONFIG,
    "subtle": Path("configs/retrieval-k1.yaml"),
    "larger": Path("configs/retrieval-k1-short.yaml"),
}
BLIND_EXPECTED: dict[str, list[str]] = {  # the family alerts pre-registered for each choice
    "unchanged": [],
    "subtle": ["model_calls"],  # about 4 times in 10
    "larger": ["model_calls", "error"],  # almost surely
}
BLIND_PATH = Path("results/online/blind.json")
BLIND_REVEAL_PATH = Path("results/online/blind-reveal.json")
blind_app = typer.Typer(
    no_args_is_help=True,
    help="The blind days 9 to 11 (phase 7 part 5): seal the choices, run a day under its sealed "
    "choice, and reveal once every verdict is in.",
)
app.add_typer(blind_app, name="blind")


def _commitment(choices: dict[int, str], passphrase: str) -> str:
    """The sealed hash: the three choices and the passphrase, which only the author knows."""
    from agent_evals.harness.cache import sha256_text

    text = ";".join(f"{d}={choices[d]}" for d in sorted(choices)) + f";pass={passphrase}"
    return sha256_text(text)


def _ask_choice(prompt: str, *, hidden: bool) -> str:
    from rich.prompt import Prompt

    return str(Prompt.ask(prompt, choices=list(BLIND_CHOICES), password=hidden))


@blind_app.command("seal")
def blind_seal(
    again: Annotated[
        bool,
        typer.Option(
            "--again",
            help="Replace a seal that no blind day has run under yet, for a forgotten choice or "
            "passphrase. The old seal is archived, never deleted.",
        ),
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Seal the choices for days 9 to 11: unchanged, subtle or larger for each, typed hidden,
    with a passphrase to repeat at the reveal. Only the hash is kept, so the choices cannot
    change once the verdicts are in, and nobody can read them before."""
    from datetime import UTC, datetime

    from rich.prompt import Prompt

    from agent_evals.store.db import ResultsStore

    if not _interactive():
        console.print("[red]evals blind seal needs an interactive terminal.[/red]")
        raise typer.Exit(code=2)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if BLIND_PATH.exists():
        if not again:
            console.print(
                f"[red]Already sealed ({BLIND_PATH}): a seal stands. If you forgot a choice or "
                "the passphrase and no blind day has run yet, pass --again.[/red]"
            )
            raise typer.Exit(code=2)
        try:  # a seal that a day ran under locks that day's choice: it cannot be replaced
            store = ResultsStore(_config(config).paths.db, read_only=True)
        except FileNotFoundError:
            ran: list[int] = []
        else:
            try:
                days = {(r["manifest"].get("online") or {}).get("day") for r in store.online_runs()}
            finally:
                store.close()
            ran = sorted(d for d in BLIND_DAYS if d in days)
        if ran:
            console.print(f"[red]Days {ran} already ran under the seal: it stands.[/red]")
            raise typer.Exit(code=2)
        stamp = now.replace(":", "").replace("+0000", "Z")
        dest = BLIND_PATH.parent / "archive" / stamp / BLIND_PATH.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        BLIND_PATH.replace(dest)
        console.print(f"The earlier seal, which no day ran under, is archived at {dest}.")
    choices = {
        d: _ask_choice(f"day {d}: unchanged, subtle or larger (hidden)", hidden=True)
        for d in BLIND_DAYS
    }
    passphrase = Prompt.ask("a passphrase, to repeat at the reveal (hidden)", password=True)
    if not passphrase.strip():
        console.print("[red]An empty passphrase seals nothing.[/red]")
        raise typer.Exit(code=2)
    if Prompt.ask("the passphrase again (hidden)", password=True) != passphrase:
        console.print("[red]The passphrases differ; nothing was sealed.[/red]")
        raise typer.Exit(code=2)
    record = {
        "sealed_at": now,
        "days": list(BLIND_DAYS),
        "options": {k: v.as_posix() for k, v in BLIND_CHOICES.items()},
        "expected_alerts": BLIND_EXPECTED,
        "commitment": _commitment(choices, passphrase),
    }
    BLIND_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLIND_PATH.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    console.print(
        f"[green]Sealed: {record['commitment']} ({BLIND_PATH}). Nothing else was kept. Next: "
        "uv run evals blind run 9[/green]"
    )


def _sealed() -> dict[str, Any]:
    """The seal, read before anything secret is typed; refused unless it holds a commitment."""
    if not BLIND_PATH.exists():
        console.print("[red]Seal the choices first: uv run evals blind seal[/red]")
        raise typer.Exit(code=2)
    try:
        sealed = json.loads(BLIND_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        sealed = None
    if not isinstance(sealed, dict) or not isinstance(sealed.get("commitment"), str):
        console.print(f"[red]{BLIND_PATH} holds no commitment: it is not a seal.[/red]")
        raise typer.Exit(code=2)
    return sealed


def _blind_db() -> Path:
    """The results database a blind day runs into: the one the three choices' configs share."""
    dbs = {_config(path).paths.db.resolve() for path in BLIND_CHOICES.values()}
    if len(dbs) != 1:
        console.print("[red]The three choices' configs name different results databases.[/red]")
        raise typer.Exit(code=2)
    return dbs.pop()


def _served_configs(db: Path, day: int) -> dict[str, str]:
    """The day's complete runs that are not shadows, oldest first, each with the config it
    recorded. Held in memory, never printed: on a blind day the config is the secret. Empty
    without a database; refused while another command holds it."""
    import duckdb

    from agent_evals import monitor as mon
    from agent_evals.store.db import ResultsStore

    try:
        store = ResultsStore(db, read_only=True)
    except FileNotFoundError:
        return {}
    except duckdb.Error:
        console.print(
            "[red]The results database is in use by another command. Nothing was done: try "
            "again once it has finished.[/red]"
        )
        raise typer.Exit(code=2) from None
    try:
        served: dict[str, str] = {}
        for run in mon.day_runs(store, day):
            role = store.run_info(run)["manifest"].get("online") or {}
            served[run] = mon._stored_path(role.get("config")).as_posix()  # as reveal reads it
        return served
    finally:
        store.close()


def _verified_choices(sealed: dict[str, Any]) -> dict[int, str]:
    """The three choices and the passphrase, typed hidden; refused unless they match the seal."""
    from rich.prompt import Prompt

    choices = {d: _ask_choice(f"day {d}'s choice (hidden)", hidden=True) for d in BLIND_DAYS}
    passphrase = Prompt.ask("the passphrase (hidden)", password=True)
    if _commitment(choices, passphrase) != sealed["commitment"]:
        console.print(
            "[red]These choices and passphrase do not match the seal. Nothing was done.[/red]"
        )
        raise typer.Exit(code=2)
    return choices


@blind_app.command("run")
def blind_run(
    day: Annotated[int, typer.Argument(help="A blind day, 9 to 11.")],
    judge_answers: Annotated[
        bool,
        typer.Option(
            "--judge/--no-judge", help="Judge the answers with the config's judge, if pinned."
        ),
    ] = True,
) -> None:
    """Run one blind day under its sealed choice. The three choices and the passphrase are typed
    hidden and checked against the seal before anything is spent, so the day runs under its
    sealed choice or not at all, and only once: a day that already ran under it is refused. The
    run's record names its config, as every run's must; it is not read until `evals monitor DAY`
    has given its verdict. With --judge, every choice's judge must be pinned, checked before
    anything is typed, so a refusal never depends on the choice."""
    if day not in BLIND_DAYS:
        console.print(f"[red]The blind days are {list(BLIND_DAYS)}.[/red]")
        raise typer.Exit(code=2)
    sealed = _sealed()
    served = _served_configs(_blind_db(), day)
    if judge_answers:  # all three, whichever the seal holds
        for path in BLIND_CHOICES.values():
            if refusal := _online_judge_refusal(_config(path).judge):
                console.print(
                    "[red]One of the blind choices' configs cannot judge an online run. "
                    f"{refusal}. Nothing was typed or run.[/red]"
                )
                raise typer.Exit(code=2)
    if not _interactive():
        console.print("[red]evals blind run needs an interactive terminal.[/red]")
        raise typer.Exit(code=2)
    chosen = BLIND_CHOICES[_verified_choices(sealed)[day]]
    if done := [run for run, recorded in served.items() if recorded == chosen.as_posix()]:
        console.print(
            f"[red]Day {day} already ran under its sealed choice ({done[0]}). Nothing was run. "
            f"Its verdict: uv run evals monitor {day}"
            + (f" --run {done[0]}" if len(served) > 1 else "")
            + "[/red]"
        )
        raise typer.Exit(code=2)
    run_id = _online_run(day=day, judge_answers=judge_answers, shadow=False, config=chosen)
    console.print(
        f"[green]Day {day} ran under its sealed choice ({run_id}). Next, before anyone reads the "
        f"run's config: uv run evals monitor {day}"
        + (f" --run {run_id}" if served else "")
        + "[/green]"
    )


@blind_app.command("match")
def blind_match(day: Annotated[int, typer.Argument(help="A blind day, 9 to 11.")]) -> None:
    """For a blind day that ran more than once: which run counts? The three choices and the
    passphrase are typed hidden and checked against the seal; the day's runs are compared with
    its sealed choice in code. Only the first run under it is named, never a choice, a config or
    how the other runs compare. That run is the day's, whatever its verdict will be."""
    if day not in BLIND_DAYS:
        console.print(f"[red]The blind days are {list(BLIND_DAYS)}.[/red]")
        raise typer.Exit(code=2)
    sealed = _sealed()
    served = _served_configs(_blind_db(), day)
    if not served:
        console.print(f"[red]Day {day} has no complete run yet: uv run evals blind run {day}[/red]")
        raise typer.Exit(code=2)
    if not _interactive():
        console.print("[red]evals blind match needs an interactive terminal.[/red]")
        raise typer.Exit(code=2)
    expected = BLIND_CHOICES[_verified_choices(sealed)[day]].as_posix()
    if not (matched := [run for run, recorded in served.items() if recorded == expected]):
        console.print(
            f"[red]No run of day {day} ran under its sealed choice. Next: uv run evals blind run "
            f"{day}, which runs it under that choice.[/red]"
        )
        raise typer.Exit(code=1)
    console.print(
        f"[green]Day {day}'s run is {matched[0]}, the first under its sealed choice. Its verdict "
        f"next: uv run evals monitor {day} --run {matched[0]}[/green]"
    )


@blind_app.command("reveal")
def blind_reveal(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """After the monitor has given every blind day its verdict: repeat the choices and the
    passphrase. The hash must match the seal; each run's recorded config is checked against the
    revealed choice; and each verdict is set against the truth."""
    from datetime import UTC, datetime

    from rich.prompt import Prompt

    from agent_evals import monitor as mon
    from agent_evals.store.db import ResultsStore

    if not BLIND_PATH.exists():
        console.print("[red]Nothing is sealed: uv run evals blind seal[/red]")
        raise typer.Exit(code=2)
    sealed = json.loads(BLIND_PATH.read_text(encoding="utf-8"))
    alerts = (
        json.loads(mon.ALERTS_PATH.read_text(encoding="utf-8")) if mon.ALERTS_PATH.exists() else {}
    )
    if missing := [d for d in BLIND_DAYS if str(d) not in alerts]:
        console.print(
            f"[red]Days {missing} have no verdict yet. The verdict comes before the reveal: "
            "uv run evals monitor DAY[/red]"
        )
        raise typer.Exit(code=2)
    if not _interactive():
        console.print("[red]evals blind reveal needs an interactive terminal.[/red]")
        raise typer.Exit(code=2)
    choices = {d: _ask_choice(f"day {d}'s choice", hidden=False) for d in BLIND_DAYS}
    passphrase = Prompt.ask("the passphrase (hidden)", password=True)
    if _commitment(choices, passphrase) != sealed["commitment"]:
        console.print("[red]These choices and passphrase do not match the seal.[/red]")
        raise typer.Exit(code=2)
    cfg = _config(config)
    store = ResultsStore(cfg.paths.db, read_only=True)
    rows: list[dict[str, Any]] = []
    try:
        for d in BLIND_DAYS:
            run = str(alerts[str(d)]["run_id"])
            recorded = (store.run_info(run)["manifest"].get("online") or {}).get("config")
            expected_cfg = BLIND_CHOICES[choices[d]].as_posix()
            raised = list(alerts[str(d)]["alerts"])
            if choices[d] == "unchanged":
                outcome = "false alarm" if raised else "quiet, as expected"
            else:
                outcome = "caught" if raised else "missed"
            rows.append(
                {
                    "day": d,
                    "choice": choices[d],
                    "run_id": run,
                    "config_recorded": recorded,
                    "config_matches": mon._stored_path(recorded).as_posix() == expected_cfg,
                    "alerts": raised,
                    "expected_alerts": BLIND_EXPECTED[choices[d]],
                    "outcome": outcome,
                }
            )
    finally:
        store.close()
    table = Table(show_header=True, header_style="bold", title="the blind days revealed")
    for col in ("day", "choice", "alerts raised", "expected", "outcome", "config as sealed"):
        table.add_column(col)
    for row in rows:
        table.add_row(
            str(row["day"]),
            row["choice"],
            ", ".join(row["alerts"]) or "none",
            ", ".join(row["expected_alerts"]) or "none",
            row["outcome"],
            "yes" if row["config_matches"] else "NO",
        )
    console.print(table)
    record = {
        "revealed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commitment": sealed["commitment"],
        "choices": {str(d): c for d, c in choices.items()},
        "days": rows,
    }
    BLIND_REVEAL_PATH.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if not all(row["config_matches"] for row in rows):
        console.print("[red]A day did not run under its sealed choice; the reveal is void.[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]Recorded in {BLIND_REVEAL_PATH}.[/green]")


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
    if s.judge_errors is not None:  # no verdict, so in no judge metric above; lower is better
        errors, judged = s.judge_errors
        share = f" ({errors / judged:.1%})" if judged else ""
        console.print(
            f"judge errors: {errors} of the {judged} answers '{judge}' judged{share} have no "
            "verdict, and are left out of its judge metrics (lower is better)"
        )
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
