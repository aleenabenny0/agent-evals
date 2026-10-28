# Codex review of commits f081f42..08ca110 (2026-10-10)

**Summary.** This is a second-opinion code review of the leaked-citations fix (commits
f081f42..08ca110), run with Codex and pasted verbatim. It raises eight findings: seven rated P2,
two of them pre-existing, and one rated P3. They cover false positives and false negatives in the
tool-markup detector, rejection loops that lose their cause, a comparison that reverses the new
metric's verdict, enforcement behavior the version fingerprint misses, a format metric that does
not establish a well-formed result, tests that do not show markup alone causes rejection, and
nearby graders that violate the architecture rules. Ruff, strict mypy and all 56 offline tests
passed; full-suite testing required an in-memory temporary-directory fixture workaround for
sandbox permissions, and no repository files changed. Triage and decisions are recorded in
section 7 of `2026-10-10-leaked-citations-fix.md` and in the commits that followed.

Second-opinion review requested with the prompt in the appendix of
`2026-10-10-leaked-citations-fix.md`. Pasted verbatim; triage and decisions are recorded in
section 7 of that walkthrough and in the commits that followed.

---

Reviewed both named commits. Existing issues are marked below.

- [P2] The regex rejects legitimate documentation examples. tool_markup.py (line 17): `\b`
  matches before a hyphen, so a cited answer containing `aws ssm get-parameter --name
  <parameter-name>` is rejected. Ordinary `<Parameter>value</Parameter>` XML also matches,
  including inside code fences. CloudFormation JSON/YAML Parameters and
  `AWS::SSM::Parameter::Value<String>` do not match by themselves. Require an actual tag
  boundary and stronger evidence of tool syntax; treating every parameter element as a tool
  call remains ambiguous.
- [P2] Detector false negatives pass both enforcement and grading. tool_markup.py (line 18),
  deterministic.py (line 129): I reproduced a submission with valid citations and
  `answer="63.</answer >"` being accepted with malformed_output=0. Namespaced field closers
  and alternative wrappers such as `<tool_call>` also escape detection. Because both components
  share the predicate, weakening it cannot be detected by the grader. Use independently labeled
  positive and negative fixtures to verify detection coverage.
- [P2] Rejection loops lose their cause and disappear from the format metric. agent.py
  (line 275), agent.py (line 306), deterministic.py (line 127): three repeated malformed
  submissions produce max_steps, empty output, and no format grade. Recovered attempts also
  score zero, so this metric measures surviving final markup rather than model formatting
  reliability. A configurable rejection cap is worth adding, with its own error label, retained
  rejection reason, and rejection/recovery metrics. Also update prompts.py (line 11): "exactly
  once" conflicts with resubmission and can make a model stop after its rejected call.
- [P2, pre-existing] Comparison reverses the new metric's verdict. cli.py (line 173),
  report.py (line 78): adding malformed_output to LOWER_IS_BETTER changes the summary label
  only. For four synthetic cases changing from 1 to 0, comparison prints difference -1,
  CI [-1, -1], "regressed", and "newly failing." Apply metric direction to verdicts and
  passing/failing case classification.
- [P2] The fingerprint includes rejection text but misses enforcement behavior. agent.py
  (line 180): including messages the model reads is correct. However, changing the detector
  pattern leaves the version unchanged. The content-rule implementation, local Pydantic
  constraints, NUDGE, and the surrounding "Rejected: ... Fix and resubmit." text also escape
  it. Include a behavior revision or implementation digest and all model-facing templates;
  update the grader's version when detection changes. The manifest's Git SHA helps for
  committed code, but a dirty flag cannot identify uncommitted behavior.
- [P3] malformed_output=0 does not establish a well-formed result. deterministic.py
  (line 129): `{}`, `{"answer": None}`, and `{"answer": ["63"]}` all score zero through
  defaulting/string coercion. A clean answer with leaked markup in citation fields also scores
  zero. Add validation of the complete output contract, possibly as a separate metric, using
  shared types rather than importing DocsAnswer. Avoid blindly applying this regex to citation
  quotes, where XML can be legitimate evidence.
- [P2] Tests do not establish that markup alone causes rejection. test_docs_agent.py
  (line 128): the leaked fixture also lacks citations. I weakened enforcement in memory to
  bypass markup checks whenever citations existed or the candidate abstained; all 11 agent
  tests still passed. Add cited-markup and abstained-markup cases, repeated
  rejection/exhaustion and final-step cases, realistic XML/placeholders, namespaced tags,
  whitespace variants, and detector fingerprint changes. `_sent_back` (line 108) is also
  brittle: it assumes the relevant tool result is first rather than matching tool_use_id.
- [P2, pre-existing] Nearby graders violate the stated architecture rules. deterministic.py
  (line 9) imports agent-owned Corpus/normalize; line 13 hard-codes the quote-verification
  threshold. More concretely, `citations=["bad"]` raises AttributeError in CitationGrader
  (line 99) and TrajectoryGrader (line 159), violating "never raises for bad agent output."
  Move shared retrieval interfaces outside agent internals, configure the threshold, and return
  failing grades for malformed citations. The new shared detector and versioned prompt
  definitions do not themselves create those violations.

Ruff, strict mypy, and all 56 offline tests passed; full-suite testing required an in-memory
temporary-directory fixture workaround for sandbox permissions. No repository files changed.
