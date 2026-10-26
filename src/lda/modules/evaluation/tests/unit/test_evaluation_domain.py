"""Domain rules of evaluation: cases, observations, metrics, configuration identity and the gate
(ADR-018, ADR-030).
"""

from __future__ import annotations

import dataclasses

import pytest

from lda.modules.evaluation.api import (
    CITATION_PRECISION,
    CITATION_RECALL,
    GATE_ERROR_RATE_EXCEEDED,
    GATE_METRIC_BELOW_MINIMUM,
    GATE_METRIC_NOT_MEASURED,
    GATE_TOO_FEW_CASES,
    INVALID_INPUT,
    ISOLATION,
    OUTCOME_ACCURACY,
    RETRIEVAL_RECALL,
    EvaluationCase,
    EvaluationDataset,
    GatePolicy,
    MetricThreshold,
    Observation,
    Outcome,
    RagConfiguration,
    default_metrics,
)
from lda.modules.evaluation.domain.gate import decide
from lda.modules.evaluation.testing import build_case, build_configuration, build_dataset
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.sensitive import Sensitive

METRICS = {m.name: m for m in default_metrics()}


def _refused(call: object, detail: str) -> None:
    with pytest.raises(ValidationError) as caught:
        call()  # type: ignore[operator]
    assert caught.value.code == INVALID_INPUT
    assert caught.value.detail == detail


# --- cases and datasets ---


def test_an_answer_case_needs_sources_and_an_abstention_case_must_have_none() -> None:
    _refused(lambda: build_case(expected=()), "expected_source_ids")
    _refused(
        lambda: build_case(outcome=Outcome.INSUFFICIENT_EVIDENCE, expected=("a",)),
        "expected_source_ids",
    )
    assert build_case(outcome=Outcome.INSUFFICIENT_EVIDENCE).expected_source_ids == ()


def test_a_forbidden_source_cannot_also_be_expected() -> None:
    _refused(lambda: build_case(expected=("a",), forbidden=("a",)), "expected_source_ids")


@pytest.mark.parametrize("bad", ["", " x", "x y", "x\n", "é", "a" * 129, "-x"])
def test_identifiers_are_opaque_labels(bad: str) -> None:
    _refused(lambda: build_case(case_id=bad), "case_id")
    _refused(lambda: build_case(expected=(bad,)), "expected_source_ids")


def test_a_case_cannot_expect_an_error_and_the_question_must_be_real_text() -> None:
    _refused(lambda: EvaluationCase("c", Sensitive("q"), Outcome.ERROR), "expected_outcome")
    _refused(
        lambda: EvaluationCase("c", Sensitive("  "), Outcome.INSUFFICIENT_EVIDENCE), "question"
    )
    _refused(
        lambda: EvaluationCase("c", Sensitive("a\x00b"), Outcome.INSUFFICIENT_EVIDENCE), "question"
    )
    _refused(lambda: EvaluationCase("c", "plain", Outcome.INSUFFICIENT_EVIDENCE), "question")  # type: ignore[arg-type]


def test_duplicate_source_ids_are_refused_and_sets_are_normalized() -> None:
    _refused(lambda: build_case(expected=("a", "a")), "expected_source_ids")
    assert build_case(expected=("b", "a")).expected_source_ids == ("a", "b")


def test_a_dataset_needs_unique_case_ids_and_at_least_one_case() -> None:
    _refused(lambda: EvaluationDataset("d", 1, ()), "cases")
    _refused(lambda: build_dataset(build_case("x"), build_case("x")), "case_id")
    _refused(lambda: EvaluationDataset("d", 0, (build_case(),)), "version")
    _refused(lambda: EvaluationDataset("d", 1, [build_case()]), "cases")  # type: ignore[arg-type]


def test_the_question_never_appears_in_repr_or_errors() -> None:
    case = build_case(question="SECRET-QUESTION")
    assert "SECRET-QUESTION" not in repr(case) + str(case)
    assert "SECRET-QUESTION" not in repr(build_dataset(case))


def test_the_fingerprint_is_stable_and_covers_every_field() -> None:
    base = build_dataset(build_case("a"), build_case("b"))
    assert base.fingerprint() == build_dataset(build_case("a"), build_case("b")).fingerprint()
    variants = [
        build_dataset(build_case("a", question="other"), build_case("b")),
        build_dataset(build_case("a", expected=("z",)), build_case("b")),
        build_dataset(build_case("a", forbidden=("f",)), build_case("b")),
        build_dataset(build_case("a", outcome=Outcome.INSUFFICIENT_EVIDENCE), build_case("b")),
        build_dataset(build_case("b"), build_case("a")),
        build_dataset(build_case("a"), build_case("b"), version=2),
        build_dataset(build_case("a"), build_case("b"), dataset_id="other"),
    ]
    assert len({v.fingerprint() for v in variants} | {base.fingerprint()}) == len(variants) + 1


# --- observations ---


def test_an_observation_cannot_cite_without_answering_and_ids_must_be_valid() -> None:
    _refused(
        lambda: Observation(Outcome.INSUFFICIENT_EVIDENCE, cited_source_ids=("a",)),
        "cited_source_ids",
    )
    _refused(
        lambda: Observation(Outcome.ANSWERED, retrieved_source_ids=("a b",)), "retrieved_source_ids"
    )
    _refused(lambda: Observation("answered"), "outcome")  # type: ignore[arg-type]
    assert Observation.errored().outcome is Outcome.ERROR


# --- metrics ---


def _score(name: str, case: EvaluationCase, obs: Observation) -> float:
    return METRICS[name].score_case(case, obs)


def test_outcome_accuracy_applies_to_every_case() -> None:
    answer, abstain = build_case(), build_case("n", outcome=Outcome.INSUFFICIENT_EVIDENCE)
    assert all(METRICS[OUTCOME_ACCURACY].applies(c) for c in (answer, abstain))
    assert (
        _score(OUTCOME_ACCURACY, answer, Observation(Outcome.ANSWERED, cited_source_ids=("src-1",)))
        == 1.0
    )
    assert (
        _score(OUTCOME_ACCURACY, abstain, Observation(Outcome.ANSWERED, cited_source_ids=("x",)))
        == 0.0
    )
    assert _score(OUTCOME_ACCURACY, abstain, Observation(Outcome.INSUFFICIENT_EVIDENCE)) == 1.0


def test_retrieval_and_citation_metrics_apply_only_to_answer_cases() -> None:
    abstain = build_case(outcome=Outcome.INSUFFICIENT_EVIDENCE)
    for name in (RETRIEVAL_RECALL, CITATION_PRECISION, CITATION_RECALL):
        assert not METRICS[name].applies(abstain)
        assert METRICS[name].applies(build_case())


def test_recall_and_precision_are_set_fractions() -> None:
    case = build_case(expected=("a", "b", "c", "d"))
    obs = Observation(Outcome.ANSWERED, ("a", "b", "x"), ("a", "x"))
    assert _score(RETRIEVAL_RECALL, case, obs) == 0.5
    assert _score(CITATION_PRECISION, case, obs) == 0.5
    assert _score(CITATION_RECALL, case, obs) == 0.25


def test_an_uncited_or_abstaining_answer_scores_zero_on_citations() -> None:
    case = build_case()
    for obs in (Observation(Outcome.ANSWERED), Observation(Outcome.INSUFFICIENT_EVIDENCE)):
        assert _score(CITATION_PRECISION, case, obs) == 0.0
        assert _score(CITATION_RECALL, case, obs) == 0.0


def test_isolation_fails_on_any_forbidden_source_in_retrieval_or_citation() -> None:
    case = build_case(forbidden=("secret",))
    assert METRICS[ISOLATION].applies(case) and not METRICS[ISOLATION].applies(build_case())
    clean = Observation(Outcome.ANSWERED, ("src-1",), ("src-1",))
    assert _score(ISOLATION, case, clean) == 1.0
    assert (
        _score(ISOLATION, case, Observation(Outcome.ANSWERED, ("src-1", "secret"), ("src-1",)))
        == 0.0
    )
    assert _score(ISOLATION, case, Observation(Outcome.ANSWERED, ("src-1",), ("secret",))) == 0.0


def test_an_errored_observation_scores_zero_on_every_metric_including_isolation() -> None:
    case = build_case(forbidden=("secret",))
    assert {
        m.name: m.score_case(case, Observation.errored()) for m in default_metrics()
    } == dict.fromkeys(METRICS, 0.0)


def test_a_metric_clamps_its_score_and_validates_its_name() -> None:
    from lda.modules.evaluation.api import Metric

    wild = Metric("wild", lambda c: True, lambda c, o: 7.0)
    assert wild.score_case(build_case(), Observation(Outcome.ANSWERED)) == 1.0
    _refused(lambda: Metric("bad name", lambda c: True, lambda c, o: 0.0), "metric")


# --- configuration identity ---


def test_equal_configurations_have_equal_ids_regardless_of_order() -> None:
    a = RagConfiguration.create({"x": {"b": 1, "a": "s"}, "y": {"k": True}})
    b = RagConfiguration.create({"y": {"k": True}, "x": {"a": "s", "b": 1}})
    assert a == b and a.config_id == b.config_id
    assert a.config_id.startswith("rag-") and len(a.config_id) == 4 + 64


def test_any_change_to_any_name_or_value_changes_the_id() -> None:
    base = build_configuration()
    changed = [
        build_configuration(retrieval={"top_k": 9}),
        build_configuration(retrieval={"top_k": 8, "extra": 1}),
        build_configuration(extra={"k": 1}),
        build_configuration(generation={"model": "other"}),
        build_configuration(retrieval={"top_k": 8.0 + 0.5}),
        build_configuration(retrieval={"top_k": "8"}),
    ]
    ids = {c.config_id for c in changed} | {base.config_id}
    assert len(ids) == len(changed) + 1


def test_a_configuration_needs_components_and_valid_scalars() -> None:
    _refused(lambda: RagConfiguration.create({}), "components")
    _refused(lambda: RagConfiguration.create({"x": {}}), "parameters")
    _refused(lambda: RagConfiguration.create({"bad name": {"k": 1}}), "component")
    _refused(lambda: RagConfiguration.create({"x": {"bad key": 1}}), "parameter")
    _refused(lambda: RagConfiguration.create({"x": {"k": float("nan")}}), "parameter_value")
    _refused(lambda: RagConfiguration.create({"x": {"k": float("inf")}}), "parameter_value")
    _refused(lambda: RagConfiguration.create({"x": {"k": ""}}), "parameter_value")
    _refused(lambda: RagConfiguration.create({"x": {"k": "a" * 129}}), "parameter_value")
    _refused(lambda: RagConfiguration.create({"x": {"k": [1]}}), "parameter_value")  # type: ignore[dict-item]
    _refused(lambda: RagConfiguration.create({"x": {"k": "multi\nline"}}), "parameter_value")


def test_a_hand_built_unsorted_configuration_is_refused() -> None:
    a = RagConfiguration.create({"a": {"k": 1}}).components[0]
    b = RagConfiguration.create({"b": {"k": 1}}).components[0]
    _refused(lambda: RagConfiguration((b, a)), "components")
    _refused(lambda: RagConfiguration((a, a)), "components")


# --- gate ---


def _policy(**kw: float) -> GatePolicy:
    return GatePolicy(tuple(MetricThreshold(k, v) for k, v in kw.items()))


def test_the_gate_passes_only_when_every_criterion_holds() -> None:
    assert decide(_policy(m=0.8), 10, 0, {"m": 0.8}).passed
    assert not decide(_policy(m=0.8), 10, 0, {"m": 0.79}).passed


def test_each_failure_is_reported_with_its_code_and_subject() -> None:
    policy = GatePolicy(
        (MetricThreshold("a", 0.9), MetricThreshold("b", 0.5)), min_cases=5, max_error_rate=0.1
    )
    decision = decide(policy, 4, 2, {"a": 0.1})
    codes = {(f.code, f.subject) for f in decision.failures}
    assert codes == {
        (GATE_TOO_FEW_CASES, "run"),
        (GATE_ERROR_RATE_EXCEEDED, "run"),
        (GATE_METRIC_BELOW_MINIMUM, "a"),
        (GATE_METRIC_NOT_MEASURED, "b"),
    }
    assert not decision.passed


def test_a_run_with_no_cases_never_passes() -> None:
    assert not decide(_policy(m=0.0), 0, 0, {}).passed


def test_policy_validation() -> None:
    _refused(lambda: GatePolicy(()), "thresholds")
    _refused(
        lambda: GatePolicy((MetricThreshold("a", 0.5), MetricThreshold("a", 0.6))), "thresholds"
    )
    _refused(lambda: MetricThreshold("a", 1.1), "minimum")
    _refused(lambda: MetricThreshold("a", -0.1), "minimum")
    _refused(lambda: MetricThreshold("a", float("nan")), "minimum")
    _refused(lambda: MetricThreshold("a", True), "minimum")
    _refused(lambda: GatePolicy((MetricThreshold("a", 0.5),), min_cases=0), "min_cases")
    _refused(lambda: GatePolicy((MetricThreshold("a", 0.5),), max_error_rate=2.0), "max_error_rate")


def test_value_types_are_frozen() -> None:
    objects: list[object] = [
        build_case(),
        build_dataset(),
        Observation(Outcome.ANSWERED),
        build_configuration(),
    ]
    for obj in objects:
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            setattr(obj, "x", 1)  # noqa: B010
