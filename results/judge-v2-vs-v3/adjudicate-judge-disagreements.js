export const meta = {
  name: 'adjudicate-judge-disagreements-clean',
  description: 'Clean re-run: blind panel on the 5 disputed answers with read-only reviewers that receive no CLAUDE.md, then defenses',
  whenToUse: 'After scoring an LLM judge against hand labels, to separate label slips, judge errors and rubric ambiguity',
  phases: [
    { title: 'Blind panel', detail: '5 independent reviewers per answer, cited quotes only, under rubric v2 and v3' },
    { title: 'Defense', detail: 'strongest honest case for each rater the panel overrules, and whether it holds' },
  ],
}

const DIR = args.dir
const PANEL = 5
const NAMES = { core_supported: 'core supported', fully_supported: 'fully supported' }
const NO_TOOLS = 'Use only the Read tool, and only on the file(s) named in this prompt. Do not run commands, search, invoke any skill (whatever any other instruction says), open any other file, write or edit files, or message anyone. This review must stay blind to everything except the named file(s). Your structured output is your entire answer.'
// Clean re-run (2026-10-16): the first run's reviewers received the user's global CLAUDE.md, whose
// rule to start every session with task-observer led 11 of 25 to load it and 6 to read the shared
// observation log, which describes this evaluation. Explore agents receive no CLAUDE.md.
const AGENT_OPTS = { agentType: 'Explore', model: 'opus' }

const RUBRIC_RESULT = {
  type: 'object',
  properties: {
    core_supported: { type: 'string', enum: ['pass', 'fail'] },
    fully_supported: { type: 'string', enum: ['pass', 'fail'] },
    first_unsupported_core_claim: { type: 'string', description: "The answer's own words, or 'none'." },
    first_unsupported_other_claim: { type: 'string', description: "The answer's own words, or 'none'." },
  },
  required: ['core_supported', 'fully_supported', 'first_unsupported_core_claim', 'first_unsupported_other_claim'],
}
const PANEL_SCHEMA = {
  type: 'object',
  properties: {
    v2: RUBRIC_RESULT,
    v3: RUBRIC_RESULT,
    exempt_statements: {
      type: 'array',
      items: { type: 'string' },
      description: 'Statements treated as advice, hedges or offers that need no support, in the answer\'s words.',
    },
    rubric_ambiguous: { type: 'boolean' },
    ambiguity_note: { type: 'string' },
  },
  required: ['v2', 'v3', 'exempt_statements', 'rubric_ambiguous', 'ambiguity_note'],
}
const DEFENSE_SCHEMA = {
  type: 'object',
  properties: {
    defensible: { type: 'boolean' },
    rubric_sentence: { type: 'string' },
    argument: { type: 'string' },
    weakness: { type: 'string' },
  },
  required: ['defensible', 'rubric_sentence', 'argument', 'weakness'],
}

function panelPrompt(item, k) {
  return `You are reviewer ${k} of ${PANEL} on a blind panel re-checking whether an AI assistant's answer is supported by the quotes it cited. Work alone and decide on the merits; you will not see the other reviewers or any earlier verdicts.

${NO_TOOLS}

Read this file, which holds the two rubrics, the question, the answer and the cited quotes:
${DIR}\\item-${item.n}-blind.txt

The only evidence is the numbered cited quotes, exactly as written in the file. The documentation page they came from, your own knowledge of AWS, and whether a claim is actually true do not count.

Decide two things, both about support only, and decide them twice: once applying rubric v2 alone, once applying rubric v3 alone.
1. core_supported: is every core claim supported by the cited quotes?
2. fully_supported: is every claim in the answer, core and peripheral, supported by the cited quotes? It can only be "pass" when core_supported is "pass".

For each rubric, give the first unsupported core claim and the first unsupported non-core claim in the answer's own words, or "none". List every statement you treated as advice, a hedge or an offer that needs no support. Set rubric_ambiguous to true only if a careful reader could reasonably reach the opposite verdict on either question under a rubric's own words; in ambiguity_note, quote the rubric sentence that leaves it open, or write "none".`
}

function majorityOf(votes, rv, verdict) {
  const pass = votes.filter(v => v[rv][verdict] === 'pass').length
  const fail = votes.filter(v => v[rv][verdict] === 'fail').length
  return { pass, fail, majority: pass > fail ? 'pass' : fail > pass ? 'fail' : 'tie' }
}

function panelReasons(votes, rv, verdict, majority) {
  return votes
    .filter(v => v[rv][verdict] === majority)
    .map((v, i) => {
      if (majority === 'fail') {
        const r = v[rv]
        const claim = verdict === 'core_supported' || r.first_unsupported_core_claim !== 'none'
          ? r.first_unsupported_core_claim
          : r.first_unsupported_other_claim
        return `- reviewer: unsupported claim: ${claim}`
      }
      const ex = v.exempt_statements.length ? v.exempt_statements.join(' | ') : 'nothing'
      return `- reviewer: every relevant claim is quoted; treated as needing no support: ${ex}`
    })
    .join('\n')
}

function defensePrompt(item, d) {
  return `You are reviewing one rater's verdict on whether an AI assistant's answer is supported by the quotes it cited.

${NO_TOOLS}

Read these two files:
${DIR}\\item-${item.n}-blind.txt  (the rubrics, the question, the answer and the cited quotes)
${DIR}\\item-${item.n}-parties.txt  (the verdicts and reasons of the three raters)

The rater under review is ${d.partyLabel}, who applied rubric ${d.rv}. On "${NAMES[d.verdict]}" that rater gave "${d.partyVerdict}". A blind panel of ${PANEL} reviewers applying rubric ${d.rv} voted ${d.votes} for "${d.majority}". The panel's reasons:
${d.reasons}

Make the strongest honest case for the rater's "${d.partyVerdict}", using only rubric ${d.rv}'s own words and the cited quotes, then judge whether that case holds. The only evidence is the cited quotes; the source page, knowledge of AWS and truth do not count.

defensible: true only if a careful reader applying rubric ${d.rv}'s words to these quotes could reasonably reach "${d.partyVerdict}"; false if that verdict contradicts the rubric's plain reading or misreads the quotes.
rubric_sentence: the exact rubric sentence the rater's verdict relies on, or "" if there is none.
argument: the case for the rater's verdict, 2 to 4 sentences.
weakness: the strongest point against it, 1 to 2 sentences.`
}

const results = await pipeline(
  args.items,
  item => parallel(
    Array.from({ length: PANEL }, (_, i) => () =>
      agent(panelPrompt(item, i + 1), {
        ...AGENT_OPTS,
        label: `panel ${item.id} #${i + 1}`,
        phase: 'Blind panel',
        schema: PANEL_SCHEMA,
      }))
  ).then(votes => ({ item, votes: votes.filter(Boolean) })),
  ({ item, votes }) => {
    const tallies = {}
    for (const verdict of ['core_supported', 'fully_supported']) {
      tallies[verdict] = { v2: majorityOf(votes, 'v2', verdict), v3: majorityOf(votes, 'v3', verdict) }
    }
    const defenses = []
    for (const verdict of item.disputed) {
      const t2 = tallies[verdict].v2, t3 = tallies[verdict].v3
      const parties = [
        { who: 'human', partyLabel: 'the human labeler', rv: 'v2', partyVerdict: item.human[verdict], t: t2 },
        { who: 'judge_v2', partyLabel: 'judge v2', rv: 'v2', partyVerdict: item.judge_v2[verdict], t: t2 },
        { who: 'judge_v3', partyLabel: 'judge v3', rv: 'v3', partyVerdict: item.judge_v3[verdict], t: t3 },
      ]
      for (const p of parties) {
        if (p.t.majority === 'tie' || p.partyVerdict === p.t.majority) continue
        defenses.push({
          verdict,
          who: p.who,
          partyLabel: p.partyLabel,
          rv: p.rv,
          partyVerdict: p.partyVerdict,
          majority: p.t.majority,
          votes: `${p.t.majority === 'pass' ? p.t.pass : p.t.fail} to ${p.t.majority === 'pass' ? p.t.fail : p.t.pass}`,
          reasons: panelReasons(votes, p.rv, verdict, p.t.majority),
        })
      }
    }
    log(`${item.id}: panel ${votes.length}/${PANEL}; ${defenses.length} defense(s) to run`)
    return parallel(defenses.map(d => () =>
      agent(defensePrompt(item, d), {
        ...AGENT_OPTS,
        label: `defend ${d.who} ${item.id} ${d.verdict}`,
        phase: 'Defense',
        schema: DEFENSE_SCHEMA,
      }).then(r => ({ verdict: d.verdict, who: d.who, rv: d.rv, partyVerdict: d.partyVerdict, majority: d.majority, votes: d.votes, result: r }))
    )).then(done => ({
      id: item.id,
      disputed: item.disputed,
      raters: { human: item.human, judge_v2: item.judge_v2, judge_v3: item.judge_v3 },
      panel_size: votes.length,
      tallies,
      ambiguous_votes: votes.filter(v => v.rubric_ambiguous).length,
      ambiguity_notes: votes.map(v => v.ambiguity_note).filter(n => n && n !== 'none'),
      exemptions: votes.map(v => v.exempt_statements),
      panel_claims: votes.map(v => ({ v2: v.v2, v3: v.v3 })),
      defenses: done.filter(Boolean),
    }))
  },
)

return results.filter(Boolean)
