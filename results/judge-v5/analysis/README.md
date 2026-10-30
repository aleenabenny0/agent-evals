# Analysis of the claim judge's first round

The files here are the figures that the judge v6 proposal (`docs/plans/judge-v6.md`) cites,
recomputed from the records of the v5 round in the folder above: the pilot, the first draft's pass,
revision 1's three passes, the choice record and the spend ledger. Nothing here was judged anew:
every figure is a re-reading of the round's saved replies with the claim judge's own code, and the
one record the pass files do not hold is replayed from the call cache. No API call was made for any
of them, and no answer outside the 125 tuning answers was read.

`figures.json` holds the figures themselves, each with its scope stated: the tuning set under v5's
named list and v6's, v2's three passes, revision 1's errors at every cell of the grid under test,
the claim scores as levels, the skeptic's confidence, the links inside quotes and in the docs
snapshot, the round's events and costs, the fail rates, the projected build check, labeled
fitted, and two rules on the blind classes of the judge's reasons (`classes.json`), simulated on
revision 1's three passes at t = 70 with s off, with the batch 2 and 3 claims by class. Written by
`scripts/judge_v6_figures.py`, which also records the sha256 of every input.

`v2-extra-passes.json` holds v2's verdicts on each of the 125 answers under the salts `judge-s2` and
`judge-s3`, the two extra passes of the choice step. They are replayed from the call cache by
`scripts/judge_v6_figures.py`: the exact request the validated judge makes is built again and
answered by its cached reply alone, through a client that refuses any miss, and the totals are
checked against the choice record before anything is written.

`classes.json` holds the blind classification of revision 1's claim reasons: the class rule each
reader fixed before reading, and for each classified claim its batch, answer key, salt, answer
span, score and reason as the pass files hold them, the first reader's class with its alternative
where one was given, the second reader's class on batch 1, and the answer's label, with a summary
by class and label per batch and the two readers' agreement. No committed script writes it: it was
assembled once from the readers' class files, which stay outside the repository, and the pass
files, with each reader's key map recomputed from the pass files and checked.

`selection-model.json` holds the selection model's output: how often a version like revision 1
looks eligible on one pass, on three, and after a choice among several equally good versions, and
how far the best-looking version's errors read below the truth. Written by
`scripts/judge_v5_selection_model.py`, whose docstring states the model in full. It is an
illustration of the process, not a measurement of any judge.
