.PHONY: setup check lint format typecheck test snapshot smoke report

setup:            ## install everything (run once)
	uv sync

check:            ## everything CI runs; one definition shared with CI (scripts/check.py)
	uv run python scripts/check.py

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest

snapshot:         ## fetch the AWS docs pages listed in configs/docs_pages.txt
	uv run evals snapshot

smoke:            ## run the docs agent on the smoke suite (uses your API key)
	uv run evals validate suites/docs/smoke.jsonl
	uv run evals run suites/docs/smoke.jsonl

report:           ## summarize the latest run
	uv run evals report
