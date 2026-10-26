"""The supported public surface of evaluation (ADR-018, ADR-030). `api.lock` pins signatures."""

from __future__ import annotations

import dataclasses

from lda.modules.evaluation import api

STEP_12_API = {
    "CASE_ERROR",
    "CITATION_PRECISION",
    "CITATION_RECALL",
    "GATE_ERROR_RATE_EXCEEDED",
    "GATE_METRIC_BELOW_MINIMUM",
    "GATE_METRIC_NOT_MEASURED",
    "GATE_TOO_FEW_CASES",
    "INVALID_INPUT",
    "ISOLATION",
    "LIMIT_EXCEEDED",
    "OBSERVATION_INVALID",
    "OUTCOME_ACCURACY",
    "RETRIEVAL_RECALL",
    "CaseResult",
    "ConfigComponent",
    "EvaluationCase",
    "EvaluationDataset",
    "EvaluationLimits",
    "EvaluationReport",
    "EvaluationTarget",
    "GateDecision",
    "GateFailure",
    "GatePolicy",
    "Metric",
    "MetricResult",
    "MetricThreshold",
    "Observation",
    "Outcome",
    "RagConfiguration",
    "RunEvaluation",
    "RunEvaluationCommand",
    "default_metrics",
}


# Step 17 (ADR-041): additive names only; every Step 12 name above is unchanged.
STEP_17_API = {
    "CONFIG_ID",
    "AnsweringIdentity",
    "ChunkingIdentity",
    "CitationsIdentity",
    "EffectiveConfiguration",
    "GenerationIdentity",
    "PromptIdentity",
    "RetrievalIdentity",
    "CheckEligibility",
    "EligibilityOutcome",
    "EvaluateConfiguration",
    "EvaluationRunResult",
    "RecordEligibility",
    "EvaluationStore",
    "EvidenceSnapshot",
    "StoredEligibility",
    "StoredRun",
    "EligibilityDecision",
    "EvaluationRecord",
    "ReleaseCriteria",
    "verify_run",
    "RELEASE_DATASET",
    "RELEASE_GATE_POLICY",
    "release_criteria",
    "PERSISTENCE_FAILED",
    "RECORD_CORRUPT",
    "RUN_EXISTS",
    "ELIGIBILITY_CONFIG_MISMATCH",
    "ELIGIBILITY_DATASET_MISMATCH",
    "ELIGIBILITY_EVIDENCE_FUTURE",
    "ELIGIBILITY_EVIDENCE_STALE",
    "ELIGIBILITY_INCOMPLETE",
    "ELIGIBILITY_INCONSISTENT",
    "ELIGIBILITY_NO_EVIDENCE",
    "ELIGIBILITY_NOT_RECORDED",
    "ELIGIBILITY_POLICY_MISMATCH",
    "ELIGIBILITY_RECENT_FAILURE",
    "ELIGIBILITY_RECORD_CORRUPT",
    "ELIGIBILITY_RUN_FAILED",
    "ELIGIBILITY_SUPERSEDED",
}
EXPECTED_API = STEP_12_API | STEP_17_API


def test_the_public_names_are_exactly_the_approved_set() -> None:
    assert set(api.__all__) == EXPECTED_API
    assert len(api.__all__) == len(set(api.__all__))
    assert all(hasattr(api, name) for name in api.__all__)


def test_error_and_gate_codes_are_the_stable_values() -> None:
    assert api.INVALID_INPUT == "evaluation_invalid_input"
    assert api.LIMIT_EXCEEDED == "evaluation_limit_exceeded"
    assert api.CASE_ERROR == "evaluation_case_error"
    assert api.OBSERVATION_INVALID == "evaluation_observation_invalid"
    assert api.GATE_METRIC_BELOW_MINIMUM == "gate_metric_below_minimum"
    assert api.GATE_METRIC_NOT_MEASURED == "gate_metric_not_measured"
    assert api.GATE_TOO_FEW_CASES == "gate_too_few_cases"
    assert api.GATE_ERROR_RATE_EXCEEDED == "gate_error_rate_exceeded"


def test_value_types_are_frozen_dataclasses() -> None:
    for name in (
        "EvaluationCase",
        "EvaluationDataset",
        "Observation",
        "RagConfiguration",
        "GatePolicy",
        "GateDecision",
        "EvaluationReport",
        "CaseResult",
        "MetricResult",
    ):
        assert getattr(api, name).__dataclass_params__.frozen, name


def test_the_report_has_no_text_or_time_field() -> None:
    fields = {f.name for f in dataclasses.fields(api.EvaluationReport)}
    assert fields == {
        "config_id",
        "dataset_id",
        "dataset_version",
        "dataset_fingerprint",
        "case_count",
        "error_count",
        "metrics",
        "cases",
        "decision",
    }
    assert {f.name for f in dataclasses.fields(api.Observation)} == {
        "outcome",
        "retrieved_source_ids",
        "cited_source_ids",
    }


def test_the_run_use_case_declares_its_markers() -> None:
    markers = {a for a in vars(api.RunEvaluation) if a.startswith("__lda_")}
    assert markers == {"__lda_authorization__", "__lda_audit__"}
