"""Routing between models: one model answers each question (docs/plans/phase-9-routing.md).

Outcomes come from attempt 1 of each model's run, so every model has one try at every question. A
router sees only what a live service could: the question, and for a cascade, the code-graded
signals on the first model's answer. The oracle reads outcomes, so it is an upper bound and not a
router. Fitting and tuning read dev only; `choose` names one router per target before test is read.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

Target = Literal["good", "correct"]
Route = tuple[str, ...]  # the models that answer, in order; the last one's answer is served


@dataclass(frozen=True)
class Outcome:
    """One model's attempt-1 answer to one question."""

    correct: bool
    good: bool  # correct, and an abstention or fully supported by the judge
    cost_usd: float
    # Visible at once, with no judge: it abstained, errored, broke the output contract or cited
    # nothing. The only outcome a router may see, and only a cascade looks.
    escalate: bool

    def value(self, target: Target) -> bool:
        return self.good if target == "good" else self.correct


@dataclass(frozen=True)
class Question:
    case_id: str
    text: str
    type: str


Matrix = Mapping[str, Mapping[str, Outcome]]  # case id -> model -> outcome


class Router(Protocol):
    @property
    def name(self) -> str: ...

    def route(self, q: Question, escalate: Callable[[str], bool]) -> Route:
        """The models that answer `q`. `escalate(model)` is that model's answer's signal."""
        ...


@dataclass(frozen=True)
class SingleModel:
    model: str

    @property
    def name(self) -> str:
        return f"always {self.model}"

    def route(self, q: Question, escalate: Callable[[str], bool]) -> Route:
        return (self.model,)


@dataclass(frozen=True)
class ByType:
    """A model per question type. The type is a label on a case, unknown for a live question, so
    this is shown for comparison and never chosen."""

    choice: Mapping[str, str]
    default: str

    name = "by type"

    def route(self, q: Question, escalate: Callable[[str], bool]) -> Route:
        return (self.choice.get(q.type, self.default),)


@dataclass(frozen=True)
class Cascade:
    """The first model answers; on its escalation signal the second answers instead. Both are
    paid for."""

    first: str
    then: str

    @property
    def name(self) -> str:
        return f"cascade {self.first} -> {self.then}"

    def route(self, q: Question, escalate: Callable[[str], bool]) -> Route:
        return (self.first, self.then) if escalate(self.first) else (self.first,)


def served(route: Route, outcomes: Mapping[str, Outcome]) -> tuple[Outcome, float]:
    """The answer served on one question and what the whole route cost."""
    if not route:
        raise ValueError("a route needs at least one model")
    return outcomes[route[-1]], sum(outcomes[m].cost_usd for m in route)


@dataclass(frozen=True)
class Result:
    name: str
    values: tuple[bool, ...]  # served outcome per question, in question order
    costs: tuple[float, ...]

    @property
    def rate(self) -> float:
        return sum(self.values) / len(self.values)

    @property
    def cost(self) -> float:
        return sum(self.costs) / len(self.costs)


def evaluate(
    router: Router, questions: Sequence[Question], matrix: Matrix, target: Target
) -> Result:
    values: list[bool] = []
    costs: list[float] = []
    for q in questions:
        outcomes = matrix[q.case_id]
        out, cost = served(router.route(q, _signals(outcomes)), outcomes)
        values.append(out.value(target))
        costs.append(cost)
    return Result(router.name, tuple(values), tuple(costs))


def _signals(outcomes: Mapping[str, Outcome]) -> Callable[[str], bool]:
    """What a live router may read about one question's answers: each one's escalation signal."""
    return lambda model: outcomes[model].escalate


def oracle(
    questions: Sequence[Question], matrix: Matrix, models: Sequence[str], target: Target
) -> Result:
    """Per question, the cheapest model (first in `models`) whose answer met the target, else the
    cheapest model. It reads the outcome, so it bounds what any router could reach."""
    values: list[bool] = []
    costs: list[float] = []
    for q in questions:
        outcomes = matrix[q.case_id]
        model = next((m for m in models if outcomes[m].value(target)), models[0])
        values.append(outcomes[model].value(target))
        costs.append(outcomes[model].cost_usd)
    return Result("oracle", tuple(values), tuple(costs))


def rates(
    questions: Sequence[Question], matrix: Matrix, models: Sequence[str], target: Target
) -> dict[str, float]:
    return {
        m: sum(matrix[q.case_id][m].value(target) for q in questions) / len(questions)
        for m in models
    }


def fit_by_type(
    questions: Sequence[Question],
    matrix: Matrix,
    models: Sequence[str],
    target: Target,
    *,
    margin: float = 0.02,
) -> ByType:
    """Per type, the cheapest model within `margin` of the type's best rate. `models` is cheapest
    first."""
    choice: dict[str, str] = {}
    for t in sorted({q.type for q in questions}):
        r = rates([q for q in questions if q.type == t], matrix, models, target)
        best = max(r.values())
        choice[t] = next(m for m in models if r[m] >= best - margin - 1e-12)
    return ByType(choice, default=models[0])


_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class TfIdf:
    """Unit-length TF-IDF vectors over the training questions' words. Smoothed idf, as in
    scikit-learn: log((1 + n) / (1 + df)) + 1, so a word in every question still counts a
    little and an unseen word counts not at all."""

    def __init__(self, texts: Sequence[str]) -> None:
        df: Counter[str] = Counter()
        for text in texts:
            df.update(set(tokens(text)))
        n = len(texts)
        self.idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items()}

    def vector(self, text: str) -> dict[str, float]:
        tf = Counter(w for w in tokens(text) if w in self.idf)
        v = {w: c * self.idf[w] for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values()))
        return {w: x / norm for w, x in v.items()} if norm else {}


def cosine(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(w, 0.0) for w, x in a.items())


class NearestNeighbours:
    """The k most similar training questions predict each model's rate on a new one, and the
    cheapest model within `tau` of the best prediction answers. Ties in similarity go to the
    lower case id, so a route never depends on dictionary order."""

    def __init__(
        self,
        train: Sequence[Question],
        matrix: Matrix,
        models: Sequence[str],
        target: Target,
        *,
        k: int,
        tau: float,
    ) -> None:
        if not 1 <= k <= len(train):
            raise ValueError("need 1 <= k <= the number of training questions")
        self.models = list(models)
        self.k = k
        self.tau = tau
        self._tfidf = TfIdf([q.text for q in train])
        self._train = [
            (
                q.case_id,
                self._tfidf.vector(q.text),
                {m: matrix[q.case_id][m].value(target) for m in models},
            )
            for q in train
        ]

    @property
    def name(self) -> str:
        return f"nearest neighbours k={self.k} tau={self.tau:g}"

    def predict(self, text: str) -> dict[str, float]:
        v = self._tfidf.vector(text)
        nearest = sorted(self._train, key=lambda t: (-cosine(v, t[1]), t[0]))[: self.k]
        return {m: sum(t[2][m] for t in nearest) / self.k for m in self.models}

    def route(self, q: Question, escalate: Callable[[str], bool]) -> Route:
        p = self.predict(q.text)
        best = max(p.values())
        return (next(m for m in self.models if p[m] >= best - self.tau - 1e-12),)


def leave_one_out(
    questions: Sequence[Question],
    matrix: Matrix,
    models: Sequence[str],
    target: Target,
    *,
    k: int,
    tau: float,
) -> Result:
    """Nearest neighbours scored on each question by a router fitted on all the others."""
    values: list[bool] = []
    costs: list[float] = []
    for i, q in enumerate(questions):
        rest = [*questions[:i], *questions[i + 1 :]]
        router = NearestNeighbours(rest, matrix, models, target, k=k, tau=tau)
        out, cost = served(router.route(q, lambda m: False), matrix[q.case_id])
        values.append(out.value(target))
        costs.append(cost)
    return Result(f"nearest neighbours k={k} tau={tau:g}", tuple(values), tuple(costs))


K_GRID = (3, 5, 9, 15)
TAU_GRID = (0.0, 0.1, 0.2)


def best_knn(
    questions: Sequence[Question], matrix: Matrix, models: Sequence[str], target: Target
) -> tuple[int, float, Result]:
    """The (k, tau) with the highest leave-one-out rate on these questions, ties to the lower
    cost, then the smaller k and tau."""
    scored = [
        (k, tau, leave_one_out(questions, matrix, models, target, k=k, tau=tau))
        for k in K_GRID
        for tau in TAU_GRID
    ]
    return min(scored, key=lambda s: (-s[2].rate, s[2].cost, s[0], s[1]))


@dataclass(frozen=True)
class Choice:
    target: Target
    always_best: str
    chosen: str  # a router's name
    reason: str


def choose(
    single: Mapping[str, Result], usable: Sequence[Result], models: Sequence[str], target: Target
) -> Choice:
    """The pre-registered choice. Always-best is the single model with the highest dev rate, ties
    to the cheaper. The chosen router is the usable one with the highest dev rate among those
    cheaper than always-best, ties to the cheaper. When always-best is already the cheapest model,
    no router can cost less, so always-best is the choice."""
    best = min(models, key=lambda m: (-single[m].rate, models.index(m)))
    if best == models[0]:
        return Choice(target, best, single[best].name, "always-best is the cheapest model")
    cheaper = [r for r in usable if r.cost < single[best].cost]
    if not cheaper:
        return Choice(target, best, single[best].name, "no usable router is cheaper")
    pick = min(cheaper, key=lambda r: (-r.rate, r.cost, r.name))
    return Choice(
        target, best, pick.name, "highest dev rate among routers cheaper than always-best"
    )
