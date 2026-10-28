# ADR 0005: Monthly docs drift check with a committed hash lock

**Status:** accepted (2026-10-10)

## Context

ADR 0003 fixed evaluation to a dated snapshot so that documentation changes cannot move a score
silently. The flip side is staleness: AWS changes defaults and limits often, and a gold answer
written against the October snapshot can become wrong while the agent keeps being graded against
it. Someone has to notice. The snapshot itself is not committed (size and the AWS docs license),
so CI cannot diff pages directly.

## Decision

- `evals snapshot` also writes `configs/docs_pages.lock.json`: one entry per page with its URL,
  title, fetch time and sha256 of the extracted text. No content, so it is committed.
- `evals snapshot --check` fetches every page in `configs/docs_pages.txt` again, compares hashes
  with the lock, and reports changed, missing and new pages plus every suite case whose
  `gold.evidence` cites a changed or missing page. Exit code 1 means drift, 0 means none, anything
  else is an error. `--report path` writes the markdown report to a file.
- `.github/workflows/docs-drift.yml` runs the check at 06:00 UTC on the first of each month and
  on demand. On drift it opens an issue labelled `docs-drift`, or comments on the open one, with
  the report. It never edits suites, gold answers or the lock.
- A human (the author) then refreshes the snapshot, re-reads each listed case against its page,
  changes a gold answer only if the documentation changed, and commits the lock with the case
  changes. The eval-integrity rule stands: never edit a gold answer to make the agent pass.
- `evals compare` notes when two runs used different snapshot fingerprints, since their scores
  are then not strictly comparable.

## Consequences

- Staleness becomes a monthly task with a precise list of what to re-check, instead of a
  silent drift in what "correct" means.
- The hash is over extracted markdown text. If a page carries volatile text (dates, counters)
  that survives the noise filters, it will show as changed every month; the fix is to extend
  the noise filters in `snapshot.py`, not to ignore the report.
- Refreshing the snapshot starts a new comparison baseline. Champion-versus-challenger
  comparisons must be run on the same snapshot.
