export const meta = {
  name: 'iteration-3-v4-diagnostic',
  description: 'Classify why the v4 challenger dev answers that fail fully supported are unsupported, with the same instrument as the v3 analysis',
  phases: [
    { title: 'Classify', detail: 'five readers over the 46 failing v4 dev answers, same instructions as the v3 analysis' },
  ],
}

const BASE = args.base
const FLAT = Array.isArray(args.batches) ? args.batches.flat() : []
if (!Array.isArray(args.batches) || !args.batches.length || FLAT.length !== args.total || new Set(FLAT).size !== FLAT.length) {
  const shape = Array.isArray(args.batches) ? JSON.stringify(args.batches.map(b => (Array.isArray(b) ? b.length : typeof b))) : typeof args.batches
  throw new Error(`args.batches must be lists covering ${args.total} distinct ids; got ${shape}`)
}
const READ_OPTS = { agentType: 'Explore', model: 'opus', effort: 'high' }
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
const CLASSIFY = { type: 'object', properties: { items: { type: 'array', items: ITEM } }, required: ['items'] }

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
const results = await parallel(args.batches.map((ids, i) => () => classify(`batch-${i + 1}.txt`, ids, `classify v4 batch ${i + 1}`)))
const items = results.flatMap(r => r || [])
const claimsOf = it => it.unsupported_claims || []
const count = (arr, f) => arr.filter(f).length
const claims = items.flatMap(it => claimsOf(it))
const byKind = {}
for (const k of KINDS) {
  byKind[k] = {
    claims: count(claims, c => c.kind === k),
    core_claims: count(claims, c => c.kind === k && c.role === 'core'),
    answers_with: count(items, it => claimsOf(it).some(c => c.kind === k)),
    answers_where_it_is_the_only_kind: count(items, it => claimsOf(it).length > 0 && claimsOf(it).every(c => c.kind === k)),
  }
}
const tally = (f, vals) => Object.fromEntries(vals.map(v => [v, count(items, it => f(it) === v)]))
const aggregate = {
  answers: items.length,
  missing: FLAT.filter(id => !items.some(x => x.id === id)),
  unsupported_claims: claims.length,
  core_claims: count(claims, c => c.role === 'core'),
  by_kind: byKind,
  judge_right: tally(it => it.judge_right, ['yes', 'no', 'unsure']),
  dispensable: tally(it => it.dispensable, ['yes', 'partly', 'no']),
  could_quote_instead: tally(it => it.could_quote_instead, ['yes', 'partly', 'no']),
}
log(`classified ${items.length} of ${FLAT.length}; ${claims.length} unsupported claims`)
return { aggregate, classifications: items }
