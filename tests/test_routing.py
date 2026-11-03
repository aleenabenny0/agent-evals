from __future__ import annotations

import pytest

from agent_evals.routing import (
    ByType,
    Cascade,
    NearestNeighbours,
    Outcome,
    Question,
    Result,
    SingleModel,
    TfIdf,
    best_knn,
    choose,
    cosine,
    evaluate,
    fit_by_type,
    leave_one_out,
    oracle,
    served,
)

MODELS = ("haiku", "sonnet", "opus")
COST = {"haiku": 0.01, "sonnet": 0.2, "opus": 0.4}


def out(model: str, good: bool, *, correct: bool | None = None, escalate: bool = False) -> Outcome:
    return Outcome(
        correct=good if correct is None else correct,
        good=good,
        cost_usd=COST[model],
        escalate=escalate,
    )


def row(haiku: bool, sonnet: bool, opus: bool, *, escalate: bool = False) -> dict[str, Outcome]:
    return {
        "haiku": out("haiku", haiku, escalate=escalate),
        "sonnet": out("sonnet", sonnet),
        "opus": out("opus", opus),
    }


def test_served_answer_is_the_last_models_and_every_model_is_paid() -> None:
    outcomes = row(False, True, False)
    answer, cost = served(("haiku", "sonnet"), outcomes)
    assert answer is outcomes["sonnet"]
    assert cost == pytest.approx(0.21)
    with pytest.raises(ValueError):
        served((), outcomes)


def test_target_picks_good_or_correct() -> None:
    o = Outcome(correct=True, good=False, cost_usd=0.0, escalate=False)
    assert o.value("correct") and not o.value("good")


def test_cascade_escalates_only_on_the_first_models_signal() -> None:
    qs = [Question("a", "q", "lookup"), Question("b", "q", "lookup")]
    matrix = {"a": row(False, True, True, escalate=True), "b": row(True, False, False)}
    r = evaluate(Cascade("haiku", "sonnet"), qs, matrix, "good")
    assert r.values == (True, True)
    assert r.costs == pytest.approx((0.21, 0.01))
    assert r.name == "cascade haiku -> sonnet"


def test_a_router_never_sees_outcomes_it_could_not_see_live() -> None:
    # Haiku fails without any signal: the cascade cannot know and serves the failure.
    qs = [Question("a", "q", "lookup")]
    r = evaluate(Cascade("haiku", "sonnet"), qs, {"a": row(False, True, True)}, "good")
    assert r.values == (False,)


def test_single_model_rate_and_cost() -> None:
    qs = [Question(c, "q", "lookup") for c in "abcd"]
    matrix = {c: row(c != "d", True, True) for c in "abcd"}
    r = evaluate(SingleModel("haiku"), qs, matrix, "good")
    assert r.rate == pytest.approx(0.75)
    assert r.cost == pytest.approx(0.01)


def test_oracle_takes_the_cheapest_model_that_met_the_target() -> None:
    qs = [Question(c, "q", "lookup") for c in "abc"]
    matrix = {
        "a": row(True, True, True),
        "b": row(False, False, True),
        "c": row(False, False, False),
    }
    r = oracle(qs, matrix, MODELS, "good")
    assert r.values == (True, True, False)
    assert r.costs == pytest.approx((0.01, 0.4, 0.01))  # none met it: the cheapest is served


def test_by_type_picks_the_cheapest_model_within_the_margin() -> None:
    qs = [Question(f"l{i}", "q", "lookup") for i in range(50)] + [
        Question(f"m{i}", "q", "multi_step") for i in range(10)
    ]
    matrix = {f"l{i}": row(i != 0, True, True) for i in range(50)}  # haiku 0.98, 2 points down
    matrix |= {f"m{i}": row(i < 5, i < 8, True) for i in range(10)}  # opus alone is best
    fitted = fit_by_type(qs, matrix, MODELS, "good", margin=0.02)
    assert fitted.choice == {"lookup": "haiku", "multi_step": "opus"}
    assert fitted.route(Question("x", "q", "unseen"), lambda m: False) == ("haiku",)


def test_tfidf_vectors_are_unit_length_and_ignore_unseen_words() -> None:
    t = TfIdf(["lambda timeout default", "s3 bucket default"])
    v = t.vector("Lambda timeout")
    assert sum(x * x for x in v.values()) == pytest.approx(1.0)
    assert t.vector("nothing seen here") == {}
    # A word in one question weighs more than a word in both.
    assert t.idf["lambda"] > t.idf["default"]
    assert cosine(v, v) == pytest.approx(1.0)


def test_nearest_neighbours_routes_by_similar_questions() -> None:
    train = [
        Question("a", "lambda timeout limit", "lookup"),
        Question("b", "lambda timeout maximum", "lookup"),
        Question("c", "s3 bucket encryption default", "lookup"),
        Question("d", "s3 bucket versioning default", "lookup"),
    ]
    matrix = {
        "a": row(False, True, True),
        "b": row(False, True, True),
        "c": row(True, True, True),
        "d": row(True, True, True),
    }
    router = NearestNeighbours(train, matrix, MODELS, "good", k=2, tau=0.0)
    assert router.route(Question("x", "lambda timeout", "lookup"), lambda m: False) == ("sonnet",)
    assert router.route(Question("y", "s3 bucket default", "lookup"), lambda m: False) == ("haiku",)
    # A wide tolerance lets the cheapest model answer even where it is predicted worse.
    loose = NearestNeighbours(train, matrix, MODELS, "good", k=2, tau=1.0)
    assert loose.route(Question("x", "lambda timeout", "lookup"), lambda m: False) == ("haiku",)
    with pytest.raises(ValueError):
        NearestNeighbours(train, matrix, MODELS, "good", k=5, tau=0.0)


def test_nearest_neighbour_ties_break_by_case_id() -> None:
    train = [Question("b", "same words", "lookup"), Question("a", "same words", "lookup")]
    matrix = {"a": row(True, True, True), "b": row(False, True, True)}
    router = NearestNeighbours(train, matrix, MODELS, "good", k=1, tau=0.0)
    assert router.route(Question("x", "same words", "lookup"), lambda m: False) == ("haiku",)


def test_leave_one_out_never_uses_the_question_being_scored() -> None:
    # Two identical questions with opposite outcomes: each is routed by the other alone.
    qs = [Question("a", "same words", "lookup"), Question("b", "same words", "lookup")]
    matrix = {"a": row(False, True, True), "b": row(True, False, False)}
    r = leave_one_out(qs, matrix, MODELS, "good", k=1, tau=0.0)
    # a is routed by b (haiku best there): haiku fails on a. b is routed by a (sonnet): fails.
    assert r.values == (False, False)


def test_best_knn_prefers_rate_then_cost() -> None:
    qs = [Question(c, f"words {c}", "lookup") for c in "abcdefghijklmnop"]
    matrix = {q.case_id: row(True, True, True) for q in qs}
    k, tau, r = best_knn(qs, matrix, MODELS, "good")
    assert r.rate == 1.0 and r.cost == pytest.approx(0.01)
    assert (k, tau) == (3, 0.0)  # every setting ties, so the smallest wins


def result(name: str, rate: float, cost: float) -> Result:
    n = 100
    good = round(rate * n)
    return Result(name, tuple([True] * good + [False] * (n - good)), tuple([cost] * n))


def test_choose_takes_the_best_router_cheaper_than_always_best() -> None:
    single = {
        "haiku": result("always haiku", 0.30, 0.01),
        "sonnet": result("always sonnet", 0.50, 0.2),
        "opus": result("always opus", 0.60, 0.4),
    }
    usable = [single["haiku"], single["sonnet"], result("cascade haiku -> sonnet", 0.55, 0.1)]
    c = choose(single, usable, MODELS, "good")
    assert (c.always_best, c.chosen) == ("opus", "cascade haiku -> sonnet")


def test_choose_breaks_rate_ties_toward_the_cheaper_router() -> None:
    single = {
        "haiku": result("always haiku", 0.50, 0.01),
        "sonnet": result("always sonnet", 0.50, 0.2),
        "opus": result("always opus", 0.60, 0.4),
    }
    c = choose(single, [single["sonnet"], single["haiku"]], MODELS, "good")
    assert c.chosen == "always haiku"


def test_choose_keeps_always_best_when_it_is_the_cheapest_model() -> None:
    single = {
        "haiku": result("always haiku", 0.98, 0.01),
        "sonnet": result("always sonnet", 0.98, 0.2),
        "opus": result("always opus", 0.97, 0.4),
    }
    c = choose(single, [result("cascade haiku -> sonnet", 0.99, 0.02)], MODELS, "correct")
    assert (c.always_best, c.chosen) == ("haiku", "always haiku")


def test_choose_keeps_always_best_when_nothing_usable_is_cheaper() -> None:
    single = {
        "haiku": result("always haiku", 0.30, 0.01),
        "sonnet": result("always sonnet", 0.60, 0.2),
        "opus": result("always opus", 0.50, 0.4),
    }
    c = choose(single, [result("cascade haiku -> opus", 0.70, 0.3)], MODELS, "good")
    assert (c.always_best, c.chosen) == ("sonnet", "always sonnet")


def test_by_type_is_named_and_not_a_live_router() -> None:
    assert ByType({}, default="haiku").name == "by type"
