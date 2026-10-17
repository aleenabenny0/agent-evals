export const meta = {
  name: 'judge-v2.1-review',
  description: 'Check a one-paragraph judge rubric fix against all 224 hand labels and attack its adoption rule',
  phases: [
    { title: 'Review', detail: 'a consistency auditor over every labeled attribution, and an adversary on the wording and the rule' },
  ],
}
const DRAFT = args.draft
const OPTS = { agentType: 'Explore', model: 'opus', effort: 'high' }
const AUDIT = {
  type: 'object',
  properties: {
    attributions_found: { type: 'integer', description: 'Labeled answers containing an attribution or reporting frame of any wording.' },
    disagreements: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          run: { type: 'string' }, case_id: { type: 'string' }, attempt: { type: 'integer' },
          phrase: { type: 'string', description: 'The attribution phrase, verbatim.' },
          labeler_fully: { type: 'string', enum: ['yes', 'no'] },
          paragraph_verdict: { type: 'string', enum: ['supported', 'unsupported', 'unclear'] },
          why: { type: 'string' },
        },
        required: ['run', 'case_id', 'attempt', 'phrase', 'labeler_fully', 'paragraph_verdict', 'why'],
      },
      description: 'Every labeled answer where applying the paragraph to its attribution would contradict the labeler (paragraph says unsupported where she passed the answer and named nothing else, or supported where her note names the attribution as the failure), or where the paragraph is unclear.',
    },
    consistent_cases: { type: 'integer' },
    amendments: { type: 'array', items: { type: 'string' } },
  },
  required: ['attributions_found', 'disagreements', 'consistent_cases', 'amendments'],
}
const ADVERSARY = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['holds', 'weakened', 'refuted'] },
    wording_problems: { type: 'array', items: { type: 'string' } },
    rule_problems: { type: 'array', items: { type: 'string' } },
    amendments: { type: 'array', items: { type: 'string' }, description: 'Smallest fixes, keeping it one paragraph and one rule.' },
  },
  required: ['verdict', 'wording_problems', 'rule_problems', 'amendments'],
}
phase('Review')
const [audit, adversary] = await parallel([
  () => agent(`Read ${DRAFT} in full. Then read the labeled data file it names (all 224 hand-labeled answers) and the dev questions file. Your job: test whether the draft paragraph matches how this labeler actually judges attributions.

Find EVERY labeled answer whose text contains an attribution or reporting frame in any wording: "the documentation says/states/lists/notes/recommends", "according to the ... docs/page/guide/table", "the X page says", "this is stated in", "the table lists", "the guide says", and similar. Use Grep or a short read-only script; do not write files. For each, apply the draft paragraph exactly as written, together with the question (from the dev questions file, by case_id) and the cited quotes, and decide whether the paragraph would make that attribution supported or unsupported. Compare with the labeler: her fully-supported label and her note. Report every case where the paragraph would contradict her, or where it is unclear. A contradiction is: the paragraph makes the attribution unsupported but she passed the answer; or her note fails the answer because of the attribution but the paragraph makes it supported. Count the consistent cases too. Then propose the smallest wording amendments that would remove the contradictions without breaking the consistent cases. Return through the structured output tool.`, { ...OPTS, label: 'consistency audit (224 labels)', phase: 'Review', schema: AUDIT }),
  () => agent(`Read ${DRAFT} in full. You are an adversary reviewing a one-paragraph clarification to a validated LLM judge's rubric and the rule for adopting it, before any money or labeling time is spent. Try to REFUTE it.

On the wording: will an LLM judge apply it consistently? Name the ambiguous terms ("name, version or scope", "adds", "reporting what the documentation says"), the loopholes (product and service names that appear in the URL, the question's own terms, page titles, generic words like "quotas page"), and any way it could make the judge stricter or looser on claims that are NOT attributions. Check it does not contradict the rest of rubric v2 (for example the clause that statements about what the documentation says are peripheral claims).

On the rule: is the fresh sample big enough for criterion (a) to mean anything, and is (a) a fair bar? Is there any circularity (labels that informed the paragraph also used to adopt it)? Are (b) and (c) well posed (non-inferiority by one per set; probes built from each judge's own passes)? What happens if (a) passes but (b) fails narrowly? Is anything missing that should be fixed in advance (for example how ties are counted, what "agreement" means when core and fully differ)?

Give the smallest amendments, keeping it one paragraph and one rule. Default to "weakened" when an objection is serious but fixable. Return through the structured output tool.`, { ...OPTS, label: 'adversary (wording and rule)', phase: 'Review', schema: ADVERSARY }),
])
return { audit, adversary }
