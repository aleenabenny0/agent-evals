export const meta = {
  name: 'iteration-3-scoping',
  description: 'Classify why 82 dev answers are not fully supported, then design, pick and stress-test one iteration-3 change',
  phases: [
    { title: 'Classify', detail: 'eight readers over the 82 failing dev answers, plus an independent second reading of 15' },
    { title: 'Design', detail: 'three designers, one mechanism each, propose one change' },
    { title: 'Decide', detail: 'a panel scores the proposals and picks one' },
    { title: 'Verify', detail: 'an adversary tries to refute the pick' },
  ],
}

const BASE = args.base
const REPO = args.repo
// Fail fast on wrong input: non-empty batches covering args.total distinct ids, and 15 re-read ids.
const FLAT = Array.isArray(args.batches) ? args.batches.flat() : []
if (!Array.isArray(args.batches) || !args.batches.length || args.batches.some(b => !Array.isArray(b) || !b.length) || FLAT.length !== args.total || new Set(FLAT).size !== FLAT.length) {
  throw new Error(`args.batches must be non-empty lists covering ${args.total} distinct ids; got sizes ${JSON.stringify((args.batches || []).map(b => (Array.isArray(b) ? b.length : b)))}`)
}
if (!Array.isArray(args.second) || args.second.length !== 15 || args.second.some(id => !FLAT.includes(id))) {
  throw new Error(`args.second must list 15 ids from the batches; got ${Array.isArray(args.second) ? args.second.length : args.second}`)
}
const OPTS = { agentType: 'Explore', model: 'opus' }
// Readers and designers at high effort: the first run's readers inherited max effort, took about
// two minutes per tool call and never finished a 28-answer batch before an interruption.
const READ_OPTS = { ...OPTS, effort: 'high' }
const KINDS = ['about_the_docs', 'headerless_row', 'unquoted_on_page', 'beyond_quote', 'not_in_read_pages', 'other']

const CLAIM = {
  type: 'object',
  properties: {
    claim: { type: 'string', description: 'The unsupported span, copied verbatim from the answer.' },
    role: { type: 'string', enum: ['core', 'peripheral'] },
    kind: { type: 'string', enum: KINDS },
    page_evidence: { type: 'string', description: 'For unquoted_on_page: the page URL and the page sentence that states the claim. Otherwise an empty string.' },
    note: { type: 'string', description: 'One sentence: why the cited quotes do not support it.' },
  },
  required: ['claim', 'role', 'kind', 'page_evidence', 'note'],
}
const ITEM = {
  type: 'object',
  properties: {
    id: { type: 'string', description: 'The item id exactly as written after ITEM, for example docs-ai-003#1.' },
    judge_right: { type: 'string', enum: ['yes', 'no', 'unsure'], description: 'yes: the answer really has at least one unsupported claim. no: every claim is supported. unsure: it turns on a close reading.' },
    unsupported_claims: { type: 'array', items: CLAIM },
    dispensable: { type: 'string', enum: ['yes', 'partly', 'no'], description: 'With every unsupported claim deleted, does the answer still answer the question completely?' },
    could_quote_instead: { type: 'string', enum: ['yes', 'partly', 'no'], description: 'Could the agent have kept every unsupported claim by quoting text on a page it had read?' },
    summary: { type: 'string', description: 'One sentence on what went wrong in this answer.' },
  },
  required: ['id', 'judge_right', 'unsupported_claims', 'dispensable', 'could_quote_instead', 'summary'],
}
const CLASSIFY = {
  type: 'object',
  properties: { items: { type: 'array', items: ITEM } },
  required: ['items'],
}

function classifyPrompt(file, ids, only) {
  return `You are doing error analysis for an evaluation project. A documentation question-answering agent answers AWS questions and must back every factual claim with verbatim quotes from pages it read. A validated LLM judge marked each answer in your file as NOT fully supported: at least one claim is not backed by the cited quotes. Your job is to find every unsupported claim in each answer and classify it, so that the next improvement targets the largest cause. Your classification is data for a decision, so precision matters more than speed.

Read first: ${BASE}/context.md (the support rubric the judge applies, and the agent's current system prompt).
Then read the whole file ${BASE}/${file} with the Read tool. Items are separated by lines of the form "=== ITEM <id> ===". Read all of it, using offset and limit if the tool truncates; do not skim. ${only ? 'Classify ' + only + ' and no others.' : 'It holds ' + ids.length + ' items; return exactly one entry per item.'}

For each item:
1. List every claim in the answer that the cited quotes do not support under the rubric, not only the one the judge named. A claim is supported when a quote states it, or when it follows from the quotes by simple arithmetic, a unit conversion or plain rewording. A table row or cell quoted without its column header supports only its literal text, unless a quoted header fragment names the column. Advice ("check the console"), hedges ("I could not confirm") and offers to help are not claims. Judge support, not truth. The human label and the labeler's note, when present, were written by a careful person applying the same rubric: treat the note as strong evidence of what is unsupported, but check it.
2. Give each unsupported claim the FIRST kind that applies, in this order:
   - about_the_docs: the claim is about the documentation itself: what it says, what it does not say, where something is documented, or a pointer to a page ("see the pricing page", "the docs do not list a default", "the cross-Region inference documentation says").
   - headerless_row: the claim rests on the meaning of a table value (which column, default versus maximum, which tier or Region) that no quoted header supplies.
   - unquoted_on_page: a page the agent read states the claim, but no cited quote contains it. Put the page URL and the page sentence in page_evidence.
   - beyond_quote: no page the agent read states it, but a cited quote is about the same thing and the claim goes further: stronger or broader wording, an added condition or qualifier, or a conclusion drawn by combining quotes beyond simple arithmetic.
   - not_in_read_pages: no page the agent read states it and no cited quote is about it: background knowledge or invention.
   - other: anything else; explain in the note.
   Deciding between unquoted_on_page, beyond_quote and not_in_read_pages requires checking the pages. Each item lists the pages the agent read or cited, with a local file for each. Use Grep on those files with 2 or 3 distinctive words of the claim (try a synonym if the first search finds nothing). Never decide this from memory or by guessing.
3. role: core if the claim is part of the direct answer to the question or a fact the answer rests on; peripheral otherwise (context, extra numbers, names of settings, recommendations, statements about the docs).
4. judge_right, dispensable and could_quote_instead as the schema defines them. If judge_right is no, unsupported_claims is empty.

Use the item ids exactly as written after ITEM. Return everything through the structured output tool.`
}

async function classify(file, ids, label) {
  const res = await agent(classifyPrompt(file, ids, ''), { ...READ_OPTS, label, phase: 'Classify', schema: CLASSIFY })
  let got = (res && res.items) || []
  const missing = ids.filter(id => !got.some(x => x.id === id))
  if (missing.length) {
    log(`${label}: ${missing.length} of ${ids.length} items missing; asking again for just those`)
    const again = await agent(classifyPrompt(file, missing, 'ONLY these items: ' + missing.join(', ')), { ...READ_OPTS, label: label + ' (missing items)', phase: 'Classify', schema: CLASSIFY })
    got = got.concat(((again && again.items) || []).filter(x => missing.includes(x.id)))
  }
  const seen = new Set()
  return got.filter(x => {
    if (!ids.includes(x.id) || seen.has(x.id)) return false
    seen.add(x.id)
    return true
  })
}

phase('Classify')
const results = await parallel([
  ...args.batches.map((ids, i) => () => classify(`batch-${i + 1}.txt`, ids, `classify batch ${i + 1}`)),
  () => classify('second-reading.txt', args.second, 'second reading of 15'),
])
const primary = results.slice(0, args.batches.length).flatMap(r => r || [])
const second = results[args.batches.length] || []
const allIds = args.batches.flat()
const coverage = {
  expected: allIds.length,
  classified: primary.length,
  missing: allIds.filter(id => !primary.some(x => x.id === id)),
  second_reading: second.length,
}
log(`classified ${primary.length} of ${allIds.length} answers; second reading ${second.length} of ${args.second.length}`)

function claimsOf(it) { return it.unsupported_claims || [] }
function aggregate(items) {
  const claims = items.flatMap(it => claimsOf(it).map(c => ({ ...c, id: it.id })))
  const count = (arr, f) => arr.filter(f).length
  const byKind = {}
  for (const k of KINDS) {
    byKind[k] = {
      claims: count(claims, c => c.kind === k),
      core_claims: count(claims, c => c.kind === k && c.role === 'core'),
      answers_with: count(items, it => claimsOf(it).some(c => c.kind === k)),
      answers_where_it_is_the_only_kind: count(items, it => claimsOf(it).length > 0 && claimsOf(it).every(c => c.kind === k)),
    }
  }
  const pairs = []
  for (let i = 0; i < KINDS.length; i++) {
    for (let j = i + 1; j < KINDS.length; j++) {
      const s = [KINDS[i], KINDS[j]]
      pairs.push({ kinds: s, answers_flipped_if_both_fixed: count(items, it => claimsOf(it).length > 0 && claimsOf(it).every(c => s.includes(c.kind))) })
    }
  }
  pairs.sort((a, b) => b.answers_flipped_if_both_fixed - a.answers_flipped_if_both_fixed)
  const tally = (f, vals) => Object.fromEntries(vals.map(v => [v, count(items, it => f(it) === v)]))
  const n = items.map(it => claimsOf(it).length)
  return {
    answers: items.length,
    unsupported_claims: claims.length,
    core_claims: count(claims, c => c.role === 'core'),
    claims_per_answer: { zero: count(n, x => x === 0), one: count(n, x => x === 1), two: count(n, x => x === 2), three_or_more: count(n, x => x >= 3) },
    by_kind: byKind,
    top_kind_pairs: pairs.slice(0, 6),
    judge_right: tally(it => it.judge_right, ['yes', 'no', 'unsure']),
    dispensable: tally(it => it.dispensable, ['yes', 'partly', 'no']),
    could_quote_instead: tally(it => it.could_quote_instead, ['yes', 'partly', 'no']),
  }
}
const agg = aggregate(primary)

function kindSet(it) { return new Set(claimsOf(it).map(c => c.kind)) }
const rel = []
for (const s of second) {
  const p = primary.find(x => x.id === s.id)
  if (!p) continue
  const a = kindSet(p)
  const b = kindSet(s)
  const inter = [...a].filter(k => b.has(k)).length
  const uni = new Set([...a, ...b]).size
  rel.push({
    id: s.id,
    primary_kinds: [...a],
    second_kinds: [...b],
    jaccard: uni ? inter / uni : 1,
    primary_claims: claimsOf(p).length,
    second_claims: claimsOf(s).length,
    same_dispensable: p.dispensable === s.dispensable,
    same_judge_right: p.judge_right === s.judge_right,
  })
}
const reliability = {
  n: rel.length,
  identical_kind_sets: rel.filter(r => r.jaccard === 1).length,
  mean_jaccard: rel.length ? rel.reduce((t, r) => t + r.jaccard, 0) / rel.length : null,
  same_dispensable: rel.filter(r => r.same_dispensable).length,
  same_judge_right: rel.filter(r => r.same_judge_right).length,
  per_item: rel,
}
log(`reliability: identical kind sets on ${reliability.identical_kind_sets} of ${reliability.n}; mean Jaccard ${reliability.mean_jaccard === null ? '-' : reliability.mean_jaccard.toFixed(2)}`)

const examples = {}
for (const k of KINDS) {
  examples[k] = primary.flatMap(it => claimsOf(it).filter(c => c.kind === k).map(c => ({ id: it.id, claim: c.claim, role: c.role, note: c.note, page_evidence: c.page_evidence }))).slice(0, 8)
}
const summaries = primary.map(it => ({ id: it.id, kinds: [...kindSet(it)], dispensable: it.dispensable, could_quote_instead: it.could_quote_instead, summary: it.summary }))

const BRIEF = `Project: an evaluation system for a documentation question-answering agent (Claude Haiku 5.5 over a 334-page AWS docs snapshot, tools search_docs, read_page, submit_answer). Improvement happens in gated iterations (ADR 0008): exactly one change per iteration, selected by config so champion and challenger run from the same code; a gate on the dev split (130 cases, 3 attempts each) requires the target metric's paired 95% interval to exclude zero, no monitored metric (correct, fabricated_citation, uncited_answer, wrong_abstention, missed_abstention, rejected_submissions) to regress with its interval, and no case type or service area to regress on correct; then the frozen test split is run once.

Target for iteration 3: judge_supported, the share of answers in which every claim is backed by a cited quote, as judged by a validated LLM judge (kappa 0.80 to 1.00 against a human on three label sets). The champion is system prompt v3 (iteration 1, which raised judge_supported from 0.114 to 0.306 on dev with the instruction to back every sentence with a quote and to cite table headers). It stands at 0.308 per case on dev (118 answered cases). Iteration 2 added a submit-time check that rejected answers stating numbers absent from the quotes: judge_supported moved +0.020 (95% CI +0.000 to +0.045, not enough) and rejected_submissions regressed from 0.021 to 0.118 because the check fired on 38 of 390 attempts, so it failed the gate. The changelog proposed for later: sentence-level coverage, or tagging each sentence with the citation that backs it.

Error analysis just done on the 82 attempt-1 dev answers that fail fully supported (of 115 answered; only 5 also fail core). Aggregate counts: ${JSON.stringify(agg)}
Inter-reader reliability on 15 re-read answers: ${JSON.stringify({ n: reliability.n, identical_kind_sets: reliability.identical_kind_sets, mean_jaccard: reliability.mean_jaccard, same_dispensable: reliability.same_dispensable, same_judge_right: reliability.same_judge_right })}
Example claims per kind: ${JSON.stringify(examples)}
One line per answer: ${JSON.stringify(summaries)}

Files you can read: ${BASE}/context.md (the judge's rubric and the v3 prompt); ${REPO}/src/agent_evals/agents/docs/prompts.py (every model-facing text, versioned); ${REPO}/src/agent_evals/agents/docs/agent.py (submission_problems is the submit-time check; tool_specs defines the tools); ${REPO}/docs/adr/0008-gated-improvement-loop.md (the loop; if the name differs, list ${REPO}/docs/adr); ${REPO}/CHANGELOG.md (iterations 1 and 2 with their numbers).

Hard constraints for any proposal:
1. Exactly one change, of the mechanism you are assigned. The v3 champion must stay selectable and untouched.
2. Improve real support, not the judge's reading of it: the change must make answers whose claims are actually backed by their quotes. A change that only rewords answers so the rubric stops counting something, without making the content better backed, is not acceptable.
3. No example may be copied from a dev or test case; the dev split is the gate. Invented examples are fine.
4. No monitored metric may regress. A check that rejects many submissions regresses rejected_submissions, as iteration 2 showed.
5. If the change alters how answers look to the judge (much longer answers, citation markers inside the answer text), the judge's number is not trusted until a person spot-checks 30 answers; say so, and prefer changes that do not.
6. Detectability: on this dev set a paired gain below about +0.03 on judge_supported is unlikely to clear the gate.`

const PROPOSAL = {
  type: 'object',
  properties: {
    name: { type: 'string' },
    mechanism: { type: 'string' },
    change: { type: 'string', description: 'The exact change, ready to implement: the full new prompt text, or the rule with its rejection message, or the new tool description or schema.' },
    config: { type: 'string', description: 'How it is selected by config: key and value.' },
    targets: { type: 'array', items: { type: 'string' }, description: 'Which claim kinds it addresses.' },
    expected_flips: { type: 'string', description: 'How many of the 82 answers it should turn fully supported, with the arithmetic from the counts and an honest discount for compliance.' },
    risks: { type: 'string', description: 'For each monitored metric and for correctness: why it would or would not regress.' },
    changes_answer_look: { type: 'string', enum: ['no', 'slightly', 'yes'] },
    falsifier: { type: 'string', description: 'What dev result would show that it does not work.' },
    why_this: { type: 'string', description: 'Why this beats other options within your mechanism.' },
  },
  required: ['name', 'mechanism', 'change', 'config', 'targets', 'expected_flips', 'risks', 'changes_answer_look', 'falsifier', 'why_this'],
}
const ANGLES = [
  { key: 'prompt', label: 'design: prompt wording', text: 'Your mechanism: a new system prompt version (v4) that starts from v3 and changes wording only; no code change beyond registering the version.' },
  { key: 'check', label: 'design: submit-time check', text: 'Your mechanism: one deterministic submit-time check added to submission_problems and selected by a config flag, the way iteration 2 added require_number_coverage. It must reject rarely enough not to regress rejected_submissions, or you must argue convincingly why it would not.' },
  { key: 'tool', label: 'design: submit tool', text: 'Your mechanism: one change to the submit_answer tool, its description or its input schema (for example, how a citation is tied to the sentence it backs), selected by config. The answer text the judge reads should stay plain prose.' },
]

phase('Design')
const proposals = (await parallel(ANGLES.map(a => () =>
  agent(`${BRIEF}\n\n${a.text}\n\nRead the files you need, then propose the single best change for your mechanism. Ground every estimate in the counts above. Be concrete enough that an engineer could implement it from your text alone. Return it through the structured output tool.`,
    { ...READ_OPTS, label: a.label, phase: 'Design', schema: PROPOSAL })
    .then(p => (p ? { ...p, angle: a.key } : null))
))).filter(Boolean)
log(`proposals: ${proposals.map(p => p.name).join(' | ')}`)

const DECISION = {
  type: 'object',
  properties: {
    scores: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          expected_gain: { type: 'integer', description: '1 to 5' },
          low_risk: { type: 'integer', description: '1 to 5, 5 = no monitored metric at risk' },
          judge_validity: { type: 'integer', description: '1 to 5, 5 = answers look the same to the judge' },
          real_support: { type: 'integer', description: '1 to 5, 5 = improves actual backing, not the judge reading' },
          defensibility: { type: 'integer', description: '1 to 5, how cleanly it can be explained and attributed' },
          comment: { type: 'string' },
        },
        required: ['name', 'expected_gain', 'low_risk', 'judge_validity', 'real_support', 'defensibility', 'comment'],
      },
    },
    winner: { type: 'string' },
    final_change: { type: 'string', description: 'The exact change to implement. Details from runners-up may be grafted only if the result is still one change of one mechanism.' },
    config: { type: 'string' },
    expected_effect: { type: 'string', description: 'Expected judge_supported change on dev with reasoning, and what would count as failure.' },
    rationale: { type: 'string' },
  },
  required: ['scores', 'winner', 'final_change', 'config', 'expected_effect', 'rationale'],
}

phase('Decide')
const decision = await agent(`${BRIEF}\n\nThree designers each proposed one change for iteration 3, one mechanism each:\n${JSON.stringify(proposals)}\n\nScore every proposal on the five criteria in the schema, then pick the one most likely to clear the gate honestly: a detectable gain in judge_supported from better-backed answers, no monitored regression, no change in how answers look to the judge, and a change that is easy to attribute and explain. You may graft a detail from a runner-up only if the result is still one change of one mechanism. Write the final change exactly, ready to implement. Return it through the structured output tool.`,
  { ...OPTS, label: 'decide', phase: 'Decide', schema: DECISION })

const VERDICT = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['holds', 'weakened', 'refuted'] },
    strongest_objection: { type: 'string' },
    objections: { type: 'array', items: { type: 'string' } },
    required_amendments: { type: 'array', items: { type: 'string' }, description: 'Changes to the final text that would answer the objections without making it two changes. Empty if none.' },
    detectable: { type: 'string', description: 'Is the expected gain larger than the gate can detect? Show the arithmetic.' },
  },
  required: ['verdict', 'strongest_objection', 'objections', 'required_amendments', 'detectable'],
}

phase('Verify')
const verdict = decision ? await agent(`${BRIEF}\n\nThe panel picked this change for iteration 3:\n${JSON.stringify(decision)}\n\nAll three proposals, for reference:\n${JSON.stringify(proposals)}\n\nYour job is to try to REFUTE the pick before money is spent on it. Attack it on: (1) whether the expected gain is real given the counts, the reliability figures and iteration 2's lesson; (2) whether it improves actual backing or only the judge's reading of answers (Goodhart); (3) each monitored metric, especially rejected_submissions, correct and wrong_abstention; (4) whether it changes how answers look to the judge; (5) whether it is really one change; (6) whether its exact wording has loopholes the agent will exploit or ambiguities it will misread. Read the repository files you need. Default to "weakened" when an objection is serious but fixable, and list the amendments that fix it. Return it through the structured output tool.`,
  { ...OPTS, label: 'adversarial check', phase: 'Verify', schema: VERDICT }) : null

return { coverage, aggregate: agg, reliability, examples, proposals, decision, verdict, classifications: primary, second_reading: second }
