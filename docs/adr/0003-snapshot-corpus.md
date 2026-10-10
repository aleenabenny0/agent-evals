# ADR 0003: Evaluate against a dated docs snapshot, not the live site

**Status:** accepted

## Context

AWS documentation changes often. If the agent read live pages, a docs update could move the
score with no change to the agent, and quote checks could fail on text that was correct when
the agent read it.

## Decision

`evals snapshot` fetches the pages listed in `configs/docs_pages.txt` once and stores them as
markdown in `pages.jsonl` with a fetch timestamp and per-page hash. The agent's tools and the
citation grader both read only this snapshot. Each run records the snapshot fingerprint.

Search is BM25 over heading-delimited chunks: no embedding service, fully deterministic.

## Consequences

- Scores are comparable across runs that share a fingerprint, and only those.
- Refreshing the snapshot is a deliberate change, evaluated like any other.
- Questions outside the snapshot are, by definition, unanswerable, which lets the suite test
  whether the agent abstains instead of answering from memory.
- Embedding search is a future experiment with a measured before-and-after, not a default.
