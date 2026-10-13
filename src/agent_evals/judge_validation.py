"""Validating the LLM judge against hand labels (docs/plans/llm-judge.md, step 1).

Labels are split into judge-dev (for tuning the judge prompt) and judge-test (for reporting,
never looked at while tuning) by a hash of the case id, so every attempt of a case lands on the
same side and the assignment cannot be nudged. Agreement is reported per verdict as Cohen's
kappa plus TPR and TNR with Wilson intervals, where the positive class is a real failure: TPR is
the share of human-failed answers the judge also fails, TNR the share of human-passed answers
the judge also passes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent_evals.harness.cache import sha256_text
from agent_evals.stats.core import Estimate, cohens_kappa, wilson_ci

LABEL_SPLITS = ("judge-dev", "judge-test")


def label_split(case_id: str) -> str:
    """Deterministic 50/50 assignment of a case to judge-dev or judge-test."""
    return LABEL_SPLITS[int(sha256_text(case_id)[:8], 16) % 2]


REVISIT_MODES = ("unnoted", "pending", "all")


def revisit_targets(labels: Sequence[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """Which of a labeler's existing labels `evals label --revisit` walks again.

    "unnoted": labels without a note, so the reasons behind a "no" can be added afterwards.
    "pending": labels made before the two-question rubric (the strict question was answered,
    the core one never asked), so a relabel can be resumed where it stopped.
    "all": every label, to re-check or re-word. Order is the stored order (case id, attempt).
    """
    if mode not in REVISIT_MODES:
        raise ValueError(f"revisit mode must be one of {REVISIT_MODES}, not {mode!r}")
    if mode == "all":
        return list(labels)
    if mode == "pending":
        return [
            x for x in labels if x.get("supported") is not None and x.get("core_supported") is None
        ]
    return [x for x in labels if not (x.get("note") or "").strip()]


@dataclass(frozen=True)
class Agreement:
    verdict: str
    n: int
    kappa: float
    # Judge catches real failures (positive = human said fail). None when no human failures.
    tpr: Estimate | None
    # Judge passes good answers (negative = human said pass). None when no human passes.
    tnr: Estimate | None
    tp: int  # human fail, judge fail
    fp: int  # human pass, judge fail
    fn: int  # human fail, judge pass
    tn: int  # human pass, judge pass


def agreement(verdict: str, pairs: Sequence[tuple[bool, bool]]) -> Agreement:
    """`pairs` are (human_pass, judge_pass) for one verdict."""
    if not pairs:
        raise ValueError("no labeled answers with a judge verdict")
    human = [h for h, _ in pairs]
    judge = [j for _, j in pairs]
    tp = sum(1 for h, j in pairs if not h and not j)
    fp = sum(1 for h, j in pairs if h and not j)
    fn = sum(1 for h, j in pairs if not h and j)
    tn = sum(1 for h, j in pairs if h and j)
    return Agreement(
        verdict=verdict,
        n=len(pairs),
        kappa=cohens_kappa(human, judge),
        tpr=wilson_ci(tp, tp + fn) if tp + fn else None,
        tnr=wilson_ci(tn, tn + fp) if tn + fp else None,
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
    )
