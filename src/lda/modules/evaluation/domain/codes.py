"""Stable, content-free codes of the evaluation boundary (ADR-018). `detail` names a field or
limit only.
"""

from __future__ import annotations

INVALID_INPUT = "evaluation_invalid_input"
LIMIT_EXCEEDED = "evaluation_limit_exceeded"
CASE_ERROR = "evaluation_case_error"
OBSERVATION_INVALID = "evaluation_observation_invalid"

GATE_METRIC_BELOW_MINIMUM = "gate_metric_below_minimum"
GATE_METRIC_NOT_MEASURED = "gate_metric_not_measured"
GATE_TOO_FEW_CASES = "gate_too_few_cases"
GATE_ERROR_RATE_EXCEEDED = "gate_error_rate_exceeded"

# Release gating (ADR-041). Failure codes name a condition, never a value.
RUN_EXISTS = "evaluation_run_exists"
RECORD_CORRUPT = "evaluation_record_corrupt"
PERSISTENCE_FAILED = "evaluation_persistence_failed"

ELIGIBILITY_NO_EVIDENCE = "eligibility_no_evidence"
ELIGIBILITY_NOT_RECORDED = "eligibility_not_recorded"
ELIGIBILITY_SUPERSEDED = "eligibility_superseded"
ELIGIBILITY_RECORD_CORRUPT = "eligibility_record_corrupt"
ELIGIBILITY_CONFIG_MISMATCH = "eligibility_config_mismatch"
ELIGIBILITY_DATASET_MISMATCH = "eligibility_dataset_mismatch"
ELIGIBILITY_POLICY_MISMATCH = "eligibility_policy_mismatch"
ELIGIBILITY_EVIDENCE_STALE = "eligibility_evidence_stale"
ELIGIBILITY_EVIDENCE_FUTURE = "eligibility_evidence_future"
ELIGIBILITY_INCOMPLETE = "eligibility_incomplete"
ELIGIBILITY_INCONSISTENT = "eligibility_inconsistent"
ELIGIBILITY_RUN_FAILED = "eligibility_run_failed"
ELIGIBILITY_RECENT_FAILURE = "eligibility_recent_failure"
