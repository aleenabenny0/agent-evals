# Walkthrough: the leaked-citations fix

Commits `f081f42` (agent guard) and `08ca110` (metric), 2026-10-10. This note explains every
changed line in plain language so the reasoning can be reproduced in an interview without the
original conversation. Terms are defined the first time they appear.

## 1. The bug

The docs agent ends its loop by calling a tool named `submit_answer`. A **tool call** is a
structured request the model makes instead of writing prose: it names a tool and supplies a
JSON object of inputs. The API checks that object against a **schema**, a description of which
fields exist and what types they have. Our schema requires `answer` (string), `citations` (list
of url + quote), `abstained` (bool) and `confidence` (number).

On smoke case `docs-smoke-003` the model produced a tool call whose `answer` string ended with:

```
...at 160 GB per upload.</answer>
<parameter name="citations">[{"url": "...upload-objects.html", "quote": "..."}, ...]
```

and whose real `citations` field was `[]`. The model had fallen back to writing its own
internal tool-call syntax as text inside one string field. Every field had the right type, so
the schema passed, the agent accepted the submission, and the citation grader flagged the
attempt as `uncited_answer`. The eval caught it, but the agent should have bounced the
submission back for a retry instead of accepting an unusable answer.

## 2. The fix at a glance

```
model turn ──► submit_answer tool call
                    │
                    ▼
            DocsAnswer.model_validate      (shape: right fields, right types)
                    │ ValidationError ──► tool error "Invalid answer: ..."  ──► model retries
                    ▼
            submission_problems()          (content: cited? no markup?)
                    │ problems ──────────► tool error "Rejected: ..."        ──► model retries
                    ▼
            accepted ──► AgentResult.output
```

Two layers: Pydantic checks the **shape**, then `submission_problems` checks two **content
rules**. Both failure paths send a tool error back to the model on the same channel, so the
model gets another turn to fix it. Retries are bounded by the existing `max_steps` setting.

A separate grader, `FormatGrader`, measures the same markup condition on the final output. It
exists so that the eval still notices if a future agent drops the guard.

## 3. File by file

### `src/agent_evals/tool_markup.py` (new)

```python
TOOL_MARKUP = re.compile(
    r"</?(?:[a-z]+:)?(?:function_calls|invoke|parameter)\b"
    r"|</(?:answer|citations|abstained|confidence)>",
    re.IGNORECASE,
)
```

A **regular expression** (regex) is a pattern for matching text. Reading left to right:

- `</?` matches `<` optionally followed by `/`, so it covers opening and closing tags.
- `(?:[a-z]+:)?` optionally matches a namespace prefix such as `antml:`. The `(?:...)` form is
  a group that matches but is not captured for later use. The trailing `?` makes it optional.
- `(?:function_calls|invoke|parameter)` is the list of tag names Claude uses for its native
  tool-call syntax. `|` means "or".
- `\b` is a **word boundary**: the tag name must end there, so `<parameter` matches but
  `<parameters` does not.
- `|</(?:answer|citations|abstained|confidence)>` is the second alternative: a closing tag named
  after one of the `submit_answer` fields. That is exactly what leaked (`</answer>`).
- `re.IGNORECASE` makes it case-insensitive, so `</PARAMETER>` is caught too.

Two adjacent string literals in Python are joined at compile time, which is why the pattern is
split across two lines with no `+`.

```python
def find_tool_markup(text: str) -> str | None:
    match = TOOL_MARKUP.search(text)
    return match.group(0) if match else None
```

Returns the matched text rather than a bool so callers can quote it: the agent puts it in the
rejection message and the grader puts it in the grade's `reason`.

Why a separate module? `AGENTS.md` forbids graders from importing agent internals. The agent
and the grader both need the detector, so it lives in a neutral place that both may import.

Why not an XML parser? The leaked text is not well-formed XML, and we only need to detect
presence, not parse structure. A small regex is easier to read and to test.

### `src/agent_evals/agents/docs/prompts.py`

```python
REJECT_UNCITED_V1 = (
    "abstained is false but citations is empty. Every answer that is not an abstention needs "
    "at least one citation (url plus a quote copied word for word) in the citations array."
)
REJECT_MARKUP_V1 = (
    "the answer field contains tool-call markup ({snippet}). Write the answer as plain prose "
    "and put citations in the citations array, never inside answer."
)
```

These are the texts the model reads when a submission is rejected. Anything the model reads is
a prompt, and this file's rule is that prompts are versioned (`_V1`) because changing them can
change behavior. `{snippet}` is a placeholder filled with `str.format` at use time.

### `src/agent_evals/agents/docs/agent.py`

**Imports.** `REJECT_MARKUP_V1`, `REJECT_UNCITED_V1` from prompts and `find_tool_markup` from
the new module.

**`submission_problems`**

```python
def submission_problems(answer: DocsAnswer) -> list[str]:
    problems: list[str] = []
    snippet = find_tool_markup(answer.answer)
    if snippet is not None:
        problems.append(REJECT_MARKUP_V1.format(snippet=snippet))
    if not answer.abstained and not answer.citations:
        problems.append(REJECT_UNCITED_V1)
    return problems
```

- Takes a `DocsAnswer`, which is the Pydantic model, so by the time this runs the shape is
  already known to be valid.
- Returns a list, not a bool, so every problem is reported at once and the model can fix them in
  one retry. On case 003 both rules fired.
- Markup is checked first because it is the more specific diagnosis.
- Abstentions are exempt from the citation rule: an honest "the snapshot does not cover this"
  has nothing to cite. `CitationGrader` already treats abstentions the same way.
- It is a module-level function rather than a method so it can be unit-tested with no agent,
  corpus or LLM.

**The version fingerprint**

```python
"rejections": [REJECT_UNCITED_V1, REJECT_MARKUP_V1],
```

`DocsAgent.version` is a **hash** (a short fixed-length digest) of everything that can change
the agent's behavior: model settings, system prompt, tool specs, step limits. The rejection
messages now go in too. The practical effect: the run after this change reports agent
`59e86eb5ebdc` instead of the baseline's `3960330eeb86`, so the two runs are distinguishable in
the results database. The system prompt and tools did not change, which matters for the cache
(section 4).

**The loop**

```python
if tu["name"] == "submit_answer":
    try:
        candidate = DocsAnswer.model_validate(tu["input"])
    except ValidationError as exc:
        text = f"Invalid answer: {exc.errors(include_url=False)}. Fix and resubmit."
        is_error = True
    else:
        problems = submission_problems(candidate)
        if problems:
            text = "Rejected: " + " ".join(problems) + " Fix and resubmit."
            is_error = True
        else:
            submission, text, is_error = candidate, "Answer received.", False
```

- `try / except / else`: the `else` block runs only when the `try` block raised nothing. It keeps
  the content check outside the `try`, so a bug in `submission_problems` would surface as a
  real exception instead of being mistaken for a validation error.
- `candidate` is the parsed submission; it only becomes `submission` once it passes both
  layers. Later code (`if submission is not None: return done(...)`) is unchanged.
- `text` and `is_error` feed the existing `tool_result` block that is appended to the
  conversation. Setting `is_error=True` is what tells the model its tool call failed. This is
  the same mechanism `read_page` uses for an unknown URL, so the model already knows how to
  react to it.
- Nothing new bounds retries. Each rejected submission uses one of `max_steps` (12 by default).

### `src/agent_evals/graders/deterministic.py`

```python
class FormatGrader:
    name = "output_format"
    version = "1"

    def grade(self, case: Case, result: AgentResult) -> list[Grade]:
        if result.error:
            return []
        snippet = find_tool_markup(str(result.output.get("answer", "")))
        return [
            Grade(
                metric="malformed_output",
                value=float(snippet is not None),
                passed=snippet is None,
                reason="" if snippet is None else f"tool-call markup in answer: {snippet!r}",
            )
        ]
```

- `name` and `version` satisfy the `Grader` **protocol** in `types.py`. A protocol is a Python
  interface: any class with these attributes and this `grade` method counts as a grader, no
  inheritance needed.
- `if result.error: return []` mirrors `CitationGrader`: when the agent failed outright there is
  no output to judge, and the `correct` metric already records the failure.
- `float(snippet is not None)` turns the bool into `1.0` or `0.0`, which is the metric value. A
  per-case value of 0 or 1 is what makes the run-level mean a rate.
- `{snippet!r}` formats with `repr`, so the matched text is shown in quotes and escapes.
- The grader never raises on bad output, as `AGENTS.md` requires. Missing `answer` key becomes
  an empty string.

Why a separate class instead of a check inside `AnswerGrader`? `AnswerGrader` is about content
(did the answer contain the required facts, did it abstain correctly). This is about form.
Keeping them apart means each grader has one job and one version number.

Why keep a metric that reads zero by construction? While the guard works, no malformed answer
reaches the output, so the rate is 0. The metric is a tripwire: if someone weakens the regex,
disables the guard, or adds a new agent without it, the number moves. The baseline run would
have scored 1/7 on it.

### `src/agent_evals/cli.py` and `src/agent_evals/report.py`

`FormatGrader()` is added to the grader list the `run` command builds, and `"malformed_output"`
is added to `LOWER_IS_BETTER` so the report table shows "lower" in its "better" column.

### Tests

All tests run offline using `ScriptedLLM` from `tests/conftest.py`, a fake LLM that replays a
fixed list of responses and records every request it receives.

`tests/test_tool_markup.py`: parametrized positives (`</answer>`, `<parameter name=...>`,
`<invoke ...>`, `</function_calls>`, `</PARAMETER>`) and negatives (`5 GB < 50 TB`,
`<bucket-name>`, an HTML link, the word "parameters" in prose, empty string). The negatives
pin down that ordinary angle brackets in AWS prose do not trigger the detector.

`tests/test_docs_agent.py`:

- `test_uncited_submission_is_rejected_then_accepted`: script = [uncited answer, good answer].
  Asserts the final output is the good answer and that the tool result sent back after the
  first attempt had `is_error` set and mentioned "citations is empty".
- `test_leaked_markup_submission_is_rejected_then_accepted`: same shape with `LEAKED_ANSWER`,
  a reconstruction of the 003 payload. Asserts exactly one error step in the trajectory and that
  the rejection text mentions both `</answer>` and the empty citations.
- `test_abstention_without_citations_is_accepted`: pins the exemption.
- `test_version_changes_with_rejection_messages`: uses pytest's `monkeypatch` to swap the
  rejection constant for a different string and asserts `version` changes. This proves the
  fingerprint really includes it without hard-coding any hash.

The helper `_sent_back(llm, 1)` reads `llm.requests[1]["messages"][-1]["content"][0]`:
the second request the agent sent, its last message (the user turn carrying tool results),
first block. That block is the `tool_result` for the rejected submission.

`tests/test_graders.py`: `FormatGrader` flags the leaked shape with value 1.0 and a reason
containing `</answer>`; a plain answer with harmless brackets passes with 0.0; an agent error
yields no grade.

### `docs/adr/0002-structured-answers.md`

A dated amendment records that schema validity is not enough and names the two content rules,
the shared detector, and the tripwire metric. ADR 0002 was the decision this extends, so an
amendment was clearer than a new ADR.

## 4. How the re-run proved it

Run `docs-20261010-182430-a355c7` against baseline `docs-20261010-174408-ae0812`.

**The cache.** Every model call is keyed by a hash of the full request plus a salt. Because the
system prompt and tools did not change, the first calls of every case were byte-identical to
the baseline and came back from the cache for free, with the same content. Six cases replayed
entirely. Case 003 replayed its first three calls, including the bad submission, and only the
calls after the first rejection were new: 25 model calls in total, 23 cached, 2 paid.

**Case 003's trajectory.** Steps 0 to 6 cached. Step 7: rejection (markup and no citations).
Step 8, paid: the model resubmitted but the answer still ended in `</answer>`. Step 9: second
rejection. Step 10, paid: clean submission with three citations, all of which verified word for
word against `upload-objects.html`. Grades: `correct` 1, `uncited_answer` 0, `malformed_output`
0, `citation_valid_rate` 1.0, `model_calls` 5, `tool_errors` 2.

Note that rejections count as `tool_errors` and add `model_calls`. That is intended, they are
a measure of error recovery, but read those two metrics with it in mind.

## 5. Why `compare` says "no detectable change"

`evals compare` does a **paired comparison**: for each case it computes candidate minus
baseline, so case difficulty cancels out. It then builds a 95% **confidence interval** (CI) on
the mean difference with the **bootstrap**: resample the 7 cases with replacement 10,000 times,
compute the mean difference each time, and take the 2.5th and 97.5th percentiles.

- On `correct` both runs are 7/7, so every difference is 0 and the CI is 0 to 0.
- On `uncited_answer` one case moved from 1 to 0. Many resamples contain zero copies of that
  case and have mean 0, so the interval's upper end sits at 0 and the verdict is "no detectable
  change" even though the direction is right.

How many cases would have to move for the CI to exclude zero? The lower bound clears zero
when fewer than 2.5% of resamples miss every moved case. With k moved cases out of 7, that
chance is (1 - k/7)^7: 34% for k = 1, 9.5% for k = 2, 2.0% for k = 3. So about 3 of 7 cases
must move the same way. A 7-case smoke suite can catch gross breakage but cannot measure a
one-case improvement; that is what the ~250-case phase 2 suite is for.

`McNemar p` is the exact McNemar test on the discordant cases (passed in one run only). With
0 and 1 discordant cases it is 1.0, the same conclusion.

**Known quirk, now HANDOFF task 1:** `compare` assumes higher is better. For `uncited_answer`
it printed "newly failing: docs-smoke-003" for a case that improved, and a CI entirely below
zero would read "regressed". The direction set `LOWER_IS_BETTER` is only used by the summary
table today.

## 6. Likely interview questions

**Why not just fix the prompt?** The prompt already says to put citations in the citations
array. Prompts reduce how often a failure happens; the guard makes the failure recoverable
when it happens anyway. Both are needed, and only the guard is testable offline.

**Why not a Pydantic validator?** A `model_validator` would have worked and kept the loop
untouched. A separate function was chosen so shape and content checks are two visibly
different stages, the rejection text can be controlled exactly, and the rule can be unit-tested
without constructing a `ValidationError`.

**Doesn't sharing the detector between guard and grader make the metric blind?** It makes the
metric zero while the guard works, by design. Its purpose is to detect the guard being removed
or weakened, not to find new leak shapes. A broader grader-only check would be a separate
decision.

**What about false positives?** A legitimate AWS answer is unlikely to contain `</answer>` or
`<invoke`. If one did, the cost is one retry with an explicit message, not a failed case.
The negatives in `test_tool_markup.py` cover ordinary brackets.

**What happens if the model never produces a clean submission?** It keeps getting rejected
until `max_steps` runs out and the attempt is recorded with error `max_steps`. That labels the
cause as a step limit rather than a formatting failure. A rejection cap with its own error
label is a possible follow-up, not done yet.

**Why did the agent version change if the prompt didn't?** The rejection messages are text the
model reads, so they are part of the behavior fingerprint. The system prompt and tool specs
are unchanged, which is why the cache still hit.

## Appendix: prompt for the Codex review

Paste this into Codex in the repo root. It reads `AGENTS.md` on its own.

```
Review commits f081f42..08ca110 on main as a second opinion. Do not edit any files. Write the
review as a markdown list of findings, most important first, each with a file:line reference
and a concrete failure scenario. Skip praise and style nits that ruff would catch.

Context: on smoke case docs-smoke-003 the Haiku model wrote its citations as tool-call markup
inside the `answer` string (ending with `</answer>` then `<parameter name="citations">[...]`)
and submitted an empty `citations` list. The schema accepted it. These commits add:
(1) `submission_problems` in src/agent_evals/agents/docs/agent.py, which rejects a
non-abstained answer with no citations or an answer containing tool-call markup and returns
the reason to the model as a tool error so it resubmits;
(2) a shared detector in src/agent_evals/tool_markup.py;
(3) `FormatGrader` in src/agent_evals/graders/deterministic.py emitting a `malformed_output`
metric, registered in cli.py and marked lower-is-better in report.py;
(4) offline tests using ScriptedLLM, and an amendment to docs/adr/0002.
The rejection messages are versioned prompts included in the agent version fingerprint.
Check the change against the rules in AGENTS.md.

Answer these specifically:
1. The regex in tool_markup.py: realistic false positives on AWS documentation prose (XML
   snippets, CloudFormation templates, Parameter Store) or false negatives on other ways the
   markup could leak?
2. Rejections retry until max_steps. Is there a failure mode where a looping model mislabels
   the cause as max_steps, and is a rejection cap with its own error label worth adding?
3. The agent and the grader share one detector, so malformed_output reads zero while the
   guard works. What blind spots does that create? Should the grader check more than the
   answer field?
4. Is putting the rejection messages in the version fingerprint correct, and does anything
   else that changes behavior still escape the fingerprint?
5. Test quality: what do the new tests not cover that they should? Is anything brittle?
6. Any violation of the architecture rules in AGENTS.md: graders importing agent internals,
   settings as constants in code, graders raising on bad output.
7. Anything a careful reader would flag that mypy --strict and ruff would not.
```

## 7. After the Codex review (commits eacb715 to 742d494)

Codex's second-opinion review is saved in `2026-10-10-codex-review.md`. Every concrete claim in
it reproduced. What changed in response, and why:

**Detector v2** (`tool_markup.py`). The first pattern matched `<parameter-name>`, a real AWS
placeholder, because `\b` treats a hyphen as a word boundary, and it matched `<Parameter>` XML
because it ignored case. It also missed `</answer >` with a space. The new pattern is
case-sensitive (Claude's syntax is lowercase, AWS XML is PascalCase), requires a `name=`
attribute on opening `invoke` and `parameter` tags, allows whitespace before `>` and an optional
namespace prefix, and recognises `<tool_call>` wrappers. `tests/test_tool_markup.py` now holds
two labeled lists, LEAKED and LEGITIMATE, including every example from the review.
`FormatGrader.version` is 2.

**Fingerprint v2** (`DocsAgent.version`). The old fingerprint missed the detector pattern, the
nudge and the tool-error wrapper texts. It now hashes the whole agent config except pricing,
every text the model can read (all of `prompts.py`), the detector pattern and the tool specs.
`test_version_changes_with_the_detector` proves a pattern change is visible.

**`_check_submission`** replaces the try/except/else inside the loop: shape check, then content
rules, returning `(answer or None, text, is_error)`. The loop body shrank to a few lines.

**Rejection cap** (`docs_agent.max_submission_rejections`, default 3). After that many rejected
submissions the attempt fails as `malformed_submission: <last reason>` instead of running out of
steps and being labelled `max_steps`. The new trajectory metric `rejected_submissions` counts how
many times the final answer was sent back, whether or not the model recovered, so formatting
reliability is measured even when the final output is clean.

**Prompt v2.** Step 3 of the system prompt said "call submit_answer exactly once", which
contradicts asking for a resubmission. Only that step and the tool description changed. The
system prompt is in every request, so the call cache no longer applies and the next smoke run is
fully paid.

**Tests that prove markup alone is rejected.** Codex showed by mutation that a guard which only
checked markup when citations were empty passed every test, because the leaked fixture had no
citations. Added: markup with valid citations, markup inside an abstention, three rejections
ending in `malformed_submission`, and tool results matched by `tool_use_id` instead of position.

**Graders never raise.** A citation entry that was not an object made `CitationGrader` and
`TrajectoryGrader` throw `AttributeError`; the runner caught it as `grader_error`, but the rule
is that graders return grades. Such entries now count as fabricated, and a non-list `citations`
field counts as none. `MIN_QUOTE_CHARS` moved to `graders.min_quote_chars` in the config, and
the run manifest records every grader's name and version.

**Corpus moved** from `agents/docs/corpus.py` to `agent_evals/corpus.py`, so graders no longer
import from the agent package. Imports only, no logic changed.

**`evals compare` direction.** `Comparison` now knows whether lower is better, decides pass per
case accordingly, and exposes `verdict`. For `uncited_answer` the earlier comparison now prints
"newly passing: docs-smoke-003".

**Not done, recorded in HANDOFF.md:** a full output-contract check (Codex P3). It needs a
decision on where shared output types live, which is best taken when a second agent exists.
