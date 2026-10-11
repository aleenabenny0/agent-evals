# Walkthrough: reliability and grounding metrics

The five additions approved on 2026-10-11, what each number means, the formula where there is
one, and where it lives. Decision record: ADR 0007. Written so each can be explained in an
interview without the original conversation.

## 1. Flip rate: the noise floor

Run the same case twice and the agent may pass once and fail once. The **flip rate** is the share
of cases where that happens. It is computed in `stats/core.py` (`flip_rate`) from the same
per-case pass lists that pass^k uses, and only over cases with at least two attempts; with one
attempt per case it is not defined and the report prints nothing.

Why it matters: `evals compare` reports a difference between two runs with a confidence interval.
If identical attempts flip 8% of outcomes on their own, a 5-point difference between prompt v2
and prompt v3 is inside the band the agent produces by itself. The interval already accounts for
sampling of cases; the flip rate shows the reader how much of the spread is the agent's own
randomness. It gets a Wilson interval because each case either flipped or did not.

A subtlety worth saying out loud: every model call is cached, so how can two attempts differ?
Because the cache key includes a salt, `case_id#attempt`. Attempt 2 is a fresh call, not a replay
of attempt 1. Re-running the whole suite later does replay from cache, which is the point of the
cache (identical re-runs are free), and is why flip rate is measured across attempts, not across
runs.

## 2. pass@k beside pass^k

Both are estimated from n attempts at a case of which c passed, then averaged over cases.

```
pass^k  = C(c, k) / C(n, k)            P(all k sampled attempts pass)     tau-bench
pass@k  = 1 - C(n - c, k) / C(n, k)    P(at least one of k passes)        Chen et al. 2021
```

`C(a, b)` is "a choose b". Both are unbiased: they count, over all ways of sampling k of the n
attempts, the fraction that satisfy the condition. Worked example, 3 attempts, 1 pass:

```
pass^2 = C(1,2)/C(3,2) = 0/3 = 0        no pair of attempts both passed
pass@2 = 1 - C(2,2)/C(3,2) = 1 - 1/3    only one of the three pairs is two failures
```

pass@1 and pass^1 are both c/n, the plain pass rate. For k above 1, pass@k goes up and pass^k
goes down. The gap is the agent's ability that a user who asks once does not get. Reporting
only pass@k flatters an agent; pass^k is what the brief calls reliability, and the report prints
both so the reader can see the gap. Code: `pass_at_k`, `mean_pass_at_k` in `stats/core.py`;
`RunSummary.pass_at_k` in `report.py`; the `pass@k` line in `_print_summary` in `cli.py`.

## 3. Repeated calls: loop detection

`TrajectoryGrader` (now version 3) turns every tool step into a signature, the tool name plus its
input serialized as JSON with sorted keys, and counts how many signatures are repeats:

```python
signatures = [(s.name, json.dumps(s.input, sort_keys=True, default=str)) for s in tool_steps]
repeated = len(signatures) - len(set(signatures))
```

Sorted keys mean `{"query": "x", "k": 5}` and `{"k": 5, "query": "x"}` are the same call. Reading
the next chunk of a page (`start=8000`) is a different call and does not count. `submit_answer`
does count: resubmitting a rejected answer unchanged is a loop. It is a count per attempt, so the
report gives it a bootstrap interval, and lower is better.

## 4. Evidence precision

`evidence_recall` was already there: of the gold evidence groups, how many did the agent read or
cite. Precision is the other half: of the distinct pages the agent read, how many were gold
pages. An agent that reads eight pages to answer from one has recall 1.0 and precision 0.125.

Two decisions. Only pages read count, not pages cited, because reading is the action being
measured. And when the agent read nothing the metric is left out rather than set to zero, because
0/0 is not a rate; this matters for the oracle baseline, which is handed its pages in the first
message and may never call `read_page`.

## 5. Unsupported entities: the deterministic grounding check

`GroundingGrader` in `graders/deterministic.py` asks one question of every final answer: does
each number and each URL in it appear somewhere the agent could have read it? "Somewhere" is the
question text, every page the agent read, and every page it cited. If not, the answer is marked
`unsupported_entity = 1` with the offending entities in the reason.

Number extraction lives in `entities.py` and is shared with the suite validator and the review
screen, so all three agree on what a number is:

```python
NUMBER = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?(?!\w)(?!\.\d)")
```

Read left to right: not preceded by a letter, digit or dot; digits; optional thousands groups;
optional decimals; not followed by a letter or digit, nor by a dot and a digit. So "S3", "EC2"
and "python3.10" are names and shed no numbers, "10.0.0.0" is not four numbers, while "/16",
"10,240", "99.9%" and a "63." ending a sentence all count. Thousands separators are removed
before comparing, so "10,240" on the page supports "10240" in an answer.

Why "unsupported" and not "fabricated": the check cannot tell an invented figure from a converted
one. A page that says "900 seconds (15 minutes)" supports both; a page that says only "900
seconds" does not support "15 minutes", and the grader flags it. That is a false positive for
hallucination and a true positive for "not on the page". The LLM judge's "supported" verdict will
settle conversions; this check runs on every attempt for free and catches the invented numbers
that matter most. It is reference-free (no gold answer needed), which is what phase 7's live
scoring needs. Abstentions and errored attempts get no grade. Numbers written as words are not
checked.

## 6. The same extraction fixes the review screen

Aleena's observation: the yellow highlight showed the quote, but facts from the gold answer often
sat in the unhighlighted text after it. Two changes, both in `review.py` and `evals review`:

- `answer_terms(case)` collects every required phrase, every standalone number and every
  digit-bearing identifier (`python3.10`, `AL2023`, `/16`) from the gold answer, minus anything
  the question itself states. `term_spans` finds them in the page text, whole-word and tolerant
  of "10,240" versus "10240", and the screen marks them cyan around the yellow quote.
- `uncovered_terms(case)` lists the answer facts no quote states, printed above the quotes, and
  `evals validate` prints the same gaps as warnings, plus a stronger one when a number in the
  gold answer is on no evidence page at all (that is a wrong gold answer, not a missing quote).

The marks are a reading aid, not a grader: they are deliberately generous. The validator warning
is the contract: every number the gold answer states should be in a quote the reviewer can see.
