# ADR 0009: One pin and one check per judge, judge errors, and recorded verdicts

**Status:** accepted (2026-10-28) with the claim judge's design (`docs/plans/judge-v5.md`,
decision 5), and built before stage A of that round. The round stopped at stage A on 2026-10-29;
its result is at the end of this record.

## Context

Phase 7's online monitor was built around one judge. `ONLINE_JUDGE_VERSION` pinned the only
judge allowed to grade online runs (v2, `2cf75fa03e09`). The judge check that decides whether
`judge_supported` may alert kept two write-once records on each reference run
(`judge_check_sample` and `judge_check`), and a run could belong to one such check. The monitor
refused a day unless every cited answer had a verdict, and `evals monitor DAY` rewrote the day's
entry in `alerts.json` each time it ran.

The judge improvement round needs more than that. The claim judge, under its own grader name
`judge-v5`, grades the five reference days beside v2 (stage C) and needs a blind check of its own
on those same runs. When its replies fail validation twice, it gives a grade that is not a
verdict. If it passes, days 6 to 11 are judged again, and that must not change a verdict already
recorded: day 7's pre-registered honest miss, and the blind days' verdicts, which were set
against their sealed choices.

## Decision

1. **One pin per judge name.** `online.ONLINE_JUDGE_PINS` maps a grader name to the one version
   it may grade online runs at. Today it holds only `judge` at `2cf75fa03e09`, and
   `ONLINE_JUDGE_VERSION` is that entry. `evals judge` on an online run, `evals online-run` and
   `evals blind run` refuse a judge config whose grader name has no pin, or whose version is not
   its pin (`online.pin_refusal`). `blind run` checks the judges of all three choices before
   anything is typed, so a refusal never depends on the hidden choice. `monitor.load_day` reads
   the grades of the judge it is asked for (the validated judge unless another is named) and
   refuses a run whose judge note under that name is not at its pin. The claim judge is pinned
   to its frozen version before stage C. A pin lets a judge grade online runs and nothing more:
   a judge's `judge_supported` joins the alert family only through its own passed check.
2. **One judge check per judge on the same runs.** The first check keeps its two records, under
   the same names, unchanged on the reference runs. Another judge's records are keyed by its
   grader name (`judge_check_sample:judge-v5` and `judge_check:judge-v5`, from
   `store.db.check_record_key`), and the store's write-once rule covers every such key
   (`is_write_once`), so the store refuses to change any judge's record once it is written, and
   `evals judge` refuses a grader named like one. `foreign_record`, the record readers
   (`recorded_sample`, `recorded_judge_check`), `verdict_digests` and `judge_standing` take the
   grader name (the validated judge by default), so a run holds one check per judge and one
   judge's check never counts for another. `pending_blind` and `blind_cases` read every judge's
   sample records: each judge's sampled answers stay held back, every judge's grades on them
   included, until that judge's own check is scored, and the dashboard shows none of any judge's
   sampled answers. The draw and the score for a judge other than v2 are not built yet.
3. **The `judge_error` grade.** A claim-judge answer whose replies fail validation twice gets a
   `judge_error` grade instead of a verdict. `load_day` accepts an answer whose only judge grade
   is `judge_error` in place of a verdict, and refuses one that holds both a verdict and a judge
   error. `day_values` leaves judge errors out of `judge_supported`'s values, the reported
   shares included, and counts them beside the cited answers. `check_day` marks the judge
   untrusted on a day where judge errors pass 2% of the cited answers, as it does for a style
   shift past 25%, and reports the count (`errors`, `cited` and `error_share` in the report's
   `judge` entry).
4. **A recorded verdict is never overwritten.** `evals monitor DAY` on a day already in
   `alerts.json` prints the recorded verdict and exits with its code (1 for an alert, 3 for not
   cleared, 0 for clear), recomputing and writing nothing, whatever the day's grades are now. A
   verdict that another command records while a check is running is not overwritten either.
   `--post-hoc` reads a recorded day again from today's grades and appends the reading to
   `results/online/post-hoc.json`, never to `alerts.json`. It is refused on a day without a
   recorded verdict, reads the run that verdict was given on unless `--run` names another,
   says on screen and in the file that it is post hoc, and exits 0 whatever it finds. A refreeze
   archives the post-hoc readings with the alerts they were read beside.

## Why

- Without a pin per name, the claim judge could grade online runs only by taking v2's pin, and a
  change of judge would then read as a change in the agent. A pin per name keeps v2 exactly as it
  was while a second judge is measured beside it, and the pin itself gives no power to alert.
- The first check's records are a final result: renaming or sharing them would reopen it. Keying
  a second judge's records by its name, under the same write-once rule, lets both checks live on
  the same runs, with neither able to touch the other.
- A day must stay readable when some answers cannot be judged, or one bad reply would block the
  monitor. Reading a judge error as a fail would raise "not supported", the alert's direction,
  and in a judge check it could never count as a false pass. Counting judge errors apart, with a
  bar on their share, keeps the day readable and the failure in view.
- Pre-registered verdicts are the evidence phase 7 rests on. Judging days 6 to 11 again after a
  pass must not be able to turn the honest miss into a catch, so any later reading of a recorded
  day is a post-hoc reading, in its own file, and reported as one.

## Consequences

- The validated judge, `judge`, keeps its pin, its record keys and its refusal message, and its
  online runs read as before. The one change it sees is the fourth decision: a recorded day is
  shown, not checked again. Reports written to `alerts.json` from now on carry the three
  judge-error fields, which show no judge errors for v2.
- A recorded day can be checked afresh only after a refreeze, which archives `alerts.json`;
  otherwise only a post-hoc reading is possible.
- When the claim judge is frozen, its version is added to `ONLINE_JUDGE_PINS` under `judge-v5`.
- Not built yet: the claim judge's draw and score under its own records (with *k*, the bars and
  sample sizes read from its sample record), their published copies, the record that names the
  online judge after a pass, and `online-run` judging with that judge's config.

## Result

The round stopped at stage A on 2026-10-29 (`docs/plans/judge-v5.md`, the result section).
Neither candidate was eligible on its three-pass means. Outside the named cases, the best version
made 3.0 errors on average on the 106 tuning answers, against a cap of 2.67 (v2's 0.67 plus 2),
and 1.67 false fails, against a cap of 1.33. Under the rules fixed in advance:

- `judge_supported` stays out of the alert family, and v2 stays the only pinned judge.
- No claim judge version was frozen, so `ONLINE_JUDGE_PINS` gains no `judge-v5` entry.
- The draw, score and online-judge record listed above as not built were not built.
- The changes this record describes stay as built: one pin and one check per judge name, the
  `judge_error` grade, and recorded verdicts that cannot be overwritten.

The round spent $6.95 of its $60 cap. No fresh label was drawn, and the reference days' 376
fresh answers are still unread.
