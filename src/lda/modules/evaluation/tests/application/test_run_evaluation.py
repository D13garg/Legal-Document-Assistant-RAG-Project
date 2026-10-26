"""`RunEvaluation` end to end over fakes: scoring, isolation of failures, gate, limits,
determinism (ADR-018).
"""

from __future__ import annotations

import pytest

from lda.modules.evaluation.api import (
    CASE_ERROR,
    GATE_ERROR_RATE_EXCEEDED,
    GATE_METRIC_BELOW_MINIMUM,
    GATE_METRIC_NOT_MEASURED,
    INVALID_INPUT,
    ISOLATION,
    LIMIT_EXCEEDED,
    OBSERVATION_INVALID,
    OUTCOME_ACCURACY,
    RETRIEVAL_RECALL,
    EvaluationDataset,
    EvaluationLimits,
    EvaluationReport,
    EvaluationTarget,
    GatePolicy,
    Metric,
    MetricThreshold,
    Observation,
    Outcome,
    RunEvaluation,
    RunEvaluationCommand,
    default_metrics,
)
from lda.modules.evaluation.testing import (
    PerfectTarget,
    ScriptedTarget,
    build_case,
    build_configuration,
    build_dataset,
    default_limits,
)
from lda.shared.kernel.errors import ValidationError

STRICT = GatePolicy(tuple(MetricThreshold(m.name, 1.0) for m in default_metrics()))


def _run(
    target: EvaluationTarget,
    dataset: EvaluationDataset,
    policy: GatePolicy = STRICT,
    limits: EvaluationLimits | None = None,
    metrics: tuple[Metric, ...] | None = None,
) -> EvaluationReport:
    runner = RunEvaluation(target, metrics or default_metrics(), limits or default_limits())
    return runner.execute(RunEvaluationCommand(dataset, build_configuration(), policy))


def _mixed() -> EvaluationDataset:
    return build_dataset(
        build_case("a", forbidden=("secret",)),
        build_case("b", expected=("x", "y")),
        build_case("c", outcome=Outcome.INSUFFICIENT_EVIDENCE),
    )


def test_a_perfect_target_passes_a_strict_gate_and_the_report_names_what_ran() -> None:
    dataset = _mixed()
    report = _run(PerfectTarget(), dataset)
    assert report.decision.passed and report.decision.failures == ()
    assert (report.case_count, report.error_count) == (3, 0)
    assert report.config_id == build_configuration().config_id
    assert (report.dataset_id, report.dataset_version) == ("synthetic", 1)
    assert report.dataset_fingerprint == dataset.fingerprint()
    assert [c.case_id for c in report.cases] == ["a", "b", "c"]  # dataset order
    means = {m.metric: (m.cases_measured, m.mean) for m in report.metrics}
    assert means[OUTCOME_ACCURACY] == (3, 1.0)
    assert means[RETRIEVAL_RECALL] == (2, 1.0)
    assert means[ISOLATION] == (1, 1.0)


def test_scores_aggregate_as_means_over_applicable_cases_only() -> None:
    dataset = build_dataset(
        build_case("a", expected=("x", "y")), build_case("b", outcome=Outcome.INSUFFICIENT_EVIDENCE)
    )
    target = ScriptedTarget(
        {
            "a": Observation(Outcome.ANSWERED, ("x",), ("x",)),
            "b": Observation(Outcome.INSUFFICIENT_EVIDENCE),
        }
    )
    report = _run(target, dataset, GatePolicy((MetricThreshold(RETRIEVAL_RECALL, 0.5),)))
    means = {m.metric: m.mean for m in report.metrics}
    assert means[RETRIEVAL_RECALL] == 0.5 and means[OUTCOME_ACCURACY] == 1.0
    assert report.decision.passed
    assert report.cases[1].scores == ((OUTCOME_ACCURACY, 1.0),)


def test_a_forbidden_source_fails_the_gate_through_isolation() -> None:
    dataset = build_dataset(build_case("a", forbidden=("secret",)))
    target = ScriptedTarget({"a": Observation(Outcome.ANSWERED, ("src-1", "secret"), ("src-1",))})
    report = _run(target, dataset, GatePolicy((MetricThreshold(ISOLATION, 1.0),)))
    assert not report.decision.passed
    assert [(f.code, f.subject) for f in report.decision.failures] == [
        (GATE_METRIC_BELOW_MINIMUM, ISOLATION)
    ]


def test_a_target_exception_is_one_errored_case_and_the_run_continues() -> None:
    dataset = _mixed()
    target = ScriptedTarget(
        {
            "a": RuntimeError("SECRET-QUESTION text from the target"),
            "b": Observation(Outcome.ANSWERED, ("x", "y"), ("x", "y")),
            "c": Observation(Outcome.INSUFFICIENT_EVIDENCE),
        }
    )
    report = _run(target, dataset)
    assert target.asked == ["a", "b", "c"]
    assert report.error_count == 1
    failed = report.cases[0]
    assert (failed.outcome, failed.error_code) == (Outcome.ERROR, CASE_ERROR)
    assert all(score == 0.0 for _, score in failed.scores) and failed.scores
    assert {f.code for f in report.decision.failures} >= {GATE_ERROR_RATE_EXCEEDED}
    assert not report.decision.passed
    assert "SECRET-QUESTION" not in repr(report)


def test_a_malformed_answer_is_an_errored_case_with_its_own_code() -> None:
    dataset = build_dataset(build_case("a"), build_case("b"), build_case("c"))
    target = ScriptedTarget(
        {
            "a": "just a string",
            "b": None,
            "c": ValidationError(
                "evaluation_invalid_input", detail="x"
            ),  # an invalid Observation raised inside the target
        }
    )
    report = _run(target, dataset)
    assert [c.error_code for c in report.cases] == [
        OBSERVATION_INVALID,
        OBSERVATION_INVALID,
        CASE_ERROR,
    ]
    assert report.error_count == 3


def test_a_tolerated_error_rate_lets_the_gate_pass() -> None:
    dataset = build_dataset(build_case("a"), build_case("b"))
    target = ScriptedTarget(
        {"a": RuntimeError("x"), "b": Observation(Outcome.ANSWERED, ("src-1",), ("src-1",))}
    )
    policy = GatePolicy((MetricThreshold(OUTCOME_ACCURACY, 0.5),), max_error_rate=0.5)
    assert _run(target, dataset, policy).decision.passed


def test_system_exit_and_keyboard_interrupt_are_not_swallowed() -> None:
    for exc in (KeyboardInterrupt(), SystemExit(1)):
        with pytest.raises(type(exc)):
            _run(ScriptedTarget({"case-1": exc}), build_dataset())


def test_a_metric_the_run_did_not_measure_fails_closed() -> None:
    dataset = build_dataset(build_case("c", outcome=Outcome.INSUFFICIENT_EVIDENCE))
    report = _run(PerfectTarget(), dataset, GatePolicy((MetricThreshold(RETRIEVAL_RECALL, 0.0),)))
    assert [(f.code, f.subject) for f in report.decision.failures] == [
        (GATE_METRIC_NOT_MEASURED, RETRIEVAL_RECALL)
    ]


def test_limits_are_checked_before_any_case_runs() -> None:
    target = ScriptedTarget({})
    with pytest.raises(ValidationError) as caught:
        _run(
            target, build_dataset(build_case("a"), build_case("b")), limits=EvaluationLimits(1, 100)
        )
    assert (caught.value.code, caught.value.detail) == (LIMIT_EXCEEDED, "max_cases")
    with pytest.raises(ValidationError) as caught:
        _run(target, build_dataset(build_case(question="x" * 11)), limits=EvaluationLimits(10, 10))
    assert (caught.value.code, caught.value.detail) == (LIMIT_EXCEEDED, "max_question_characters")
    assert target.asked == []


def test_a_threshold_for_an_unknown_metric_is_refused_before_any_case_runs() -> None:
    target = ScriptedTarget({})
    with pytest.raises(ValidationError) as caught:
        _run(target, build_dataset(), GatePolicy((MetricThreshold("outcome_acuracy", 1.0),)))
    assert (caught.value.code, caught.value.detail) == (INVALID_INPUT, "thresholds")
    assert target.asked == []


def test_the_metric_set_must_be_non_empty_and_unique() -> None:
    first = default_metrics()[0]
    for metrics in ((), (first, first)):
        with pytest.raises(ValidationError):
            RunEvaluation(PerfectTarget(), metrics, default_limits())


def test_a_custom_metric_can_be_registered_and_gated() -> None:
    always_half = Metric("half", lambda c: True, lambda c, o: 0.5)
    report = _run(
        PerfectTarget(),
        build_dataset(),
        GatePolicy((MetricThreshold("half", 0.6),)),
        metrics=(always_half,),
    )
    assert [f.code for f in report.decision.failures] == [GATE_METRIC_BELOW_MINIMUM]


def test_equal_inputs_give_equal_reports() -> None:
    dataset = _mixed()
    assert _run(PerfectTarget(), dataset) == _run(PerfectTarget(), dataset)


def test_the_report_carries_no_question_text() -> None:
    dataset = build_dataset(build_case(question="DISTINCTIVE-QUESTION-TEXT"))
    assert "DISTINCTIVE-QUESTION-TEXT" not in repr(_run(PerfectTarget(), dataset))
