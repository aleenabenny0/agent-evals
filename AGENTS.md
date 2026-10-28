# Instructions for coding agents (Claude Code, Codex)

agent-evals measures tool-using LLM agents for accuracy and hallucination, attributes their
failures, and improves them only through changes proven on held-out data. See README.md for
the roadmap; docs/adr/ explains past decisions.

## Commands

- Install: `uv sync`
- Everything CI runs: `uv run python scripts/check.py` (ruff, ruff format, mypy --strict,
  pytest, suite validation). `make check` is the same thing; `--fix` auto-formats first. Run it
  before saying a task is done, and fix what it reports.
- One test file: `uv run pytest tests/test_graders.py -q`
- Validate a suite: `uv run evals validate suites/docs/smoke.jsonl`
- **Costs money, ask first:** `uv run evals run ...` and `make smoke` call the Claude API.
  Never run them without the user saying yes in this session.

## Architecture rules

- The harness talks to agents only through the `Agent` protocol in `src/agent_evals/types.py`.
  Never import an agent's internals from `harness/`, `graders/`, `stats/` or `store/`.
- Every model call goes through `harness/llm.py` (`LLMClient`), so it is cached and timed.
  Keep the per-attempt cache salt (docs/adr/0001).
- Graders: deterministic code first; an LLM judge only for what code can't settle. A grader
  returns `Grade` objects and never raises for bad agent output.
- Settings live in `configs/*.yaml` via `config.py`, not as constants in code.
- The docs agent and graders read only the snapshot in `data/snapshots/`, never the live web.
- A significant design decision gets a short ADR in `docs/adr/` (next number, same format).

## Eval integrity (do not break these)

- Never use `split: "test"` cases to write prompts, lessons, examples or fixes. Learning code
  may only read `dev`.
- Never edit a gold answer to make the agent pass. If a gold answer looks wrong, stop and tell
  the user, citing the page that contradicts it.
- New cases written by an agent go in a file ending `.drafts.jsonl` with
  `"provenance": "synthetic"`. The user verifies them before they move into a real suite.
- Report results with their confidence intervals; never claim an improvement the CI doesn't show.

## Code style

- Python 3.12, full type hints, `mypy --strict` clean, ruff line length 100.
- Pydantic models for data that crosses module boundaries; dataclasses for small internals.
- Tests run offline: use `ScriptedLLM` from `tests/conftest.py` and synthetic pages. Never call
  the real API, the network, or AWS from tests.
- Small, focused commits with messages that say why.

## Safety

- Never read, print or commit `.env` or any API key or credential.
- Never use real AWS credentials or touch a real AWS account. Generated boto3 code runs only
  against moto in the no-network sandbox (code agent phase).
- Don't delete `.cache/`, `results/` or `data/snapshots/` unless asked.

## Changes to graders and statistics

In `graders/` and `stats/`, write up the approach and have it agreed before writing code, keep
changes small, and add a test for each behavior. Prefer clear code over clever code everywhere.
