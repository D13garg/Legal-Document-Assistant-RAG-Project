"""Public surface of evaluation (ADR-018, ADR-030): cases, metrics, configuration identity, gate
and run.

Evaluation is offline and tenant-less. It takes an injected `EvaluationTarget`, never imports
another
module, calls no provider, builds no scope, proof or context, and persists nothing. A report holds
identifiers, counts and scores only.
"""

from lda.modules.evaluation.application.dto import RunEvaluationCommand
from lda.modules.evaluation.application.limits import EvaluationLimits
from lda.modules.evaluation.application.ports import EvaluationTarget
from lda.modules.evaluation.application.release_dto import EligibilityOutcome, EvaluationRunResult
from lda.modules.evaluation.application.release_ports import (
    EvaluationStore,
    EvidenceSnapshot,
    StoredEligibility,
    StoredRun,
)
from lda.modules.evaluation.application.use_cases.release_gating import (
    CheckEligibility,
    EvaluateConfiguration,
    RecordEligibility,
)
from lda.modules.evaluation.application.use_cases.run_evaluation import RunEvaluation
from lda.modules.evaluation.domain.codes import (
    CASE_ERROR,
    ELIGIBILITY_CONFIG_MISMATCH,
    ELIGIBILITY_DATASET_MISMATCH,
    ELIGIBILITY_EVIDENCE_FUTURE,
    ELIGIBILITY_EVIDENCE_STALE,
    ELIGIBILITY_INCOMPLETE,
    ELIGIBILITY_INCONSISTENT,
    ELIGIBILITY_NO_EVIDENCE,
    ELIGIBILITY_NOT_RECORDED,
    ELIGIBILITY_POLICY_MISMATCH,
    ELIGIBILITY_RECENT_FAILURE,
    ELIGIBILITY_RECORD_CORRUPT,
    ELIGIBILITY_RUN_FAILED,
    ELIGIBILITY_SUPERSEDED,
    GATE_ERROR_RATE_EXCEEDED,
    GATE_METRIC_BELOW_MINIMUM,
    GATE_METRIC_NOT_MEASURED,
    GATE_TOO_FEW_CASES,
    INVALID_INPUT,
    LIMIT_EXCEEDED,
    OBSERVATION_INVALID,
    PERSISTENCE_FAILED,
    RECORD_CORRUPT,
    RUN_EXISTS,
)
from lda.modules.evaluation.domain.configuration import ConfigComponent, RagConfiguration
from lda.modules.evaluation.domain.dataset import EvaluationCase, EvaluationDataset
from lda.modules.evaluation.domain.effective_configuration import (
    CONFIG_ID,
    AnsweringIdentity,
    ChunkingIdentity,
    CitationsIdentity,
    EffectiveConfiguration,
    GenerationIdentity,
    PromptIdentity,
    RetrievalIdentity,
)
from lda.modules.evaluation.domain.gate import (
    GateDecision,
    GateFailure,
    GatePolicy,
    MetricThreshold,
)
from lda.modules.evaluation.domain.metrics import (
    CITATION_PRECISION,
    CITATION_RECALL,
    ISOLATION,
    OUTCOME_ACCURACY,
    RETRIEVAL_RECALL,
    Metric,
    default_metrics,
)
from lda.modules.evaluation.domain.observation import Observation
from lda.modules.evaluation.domain.outcome import Outcome
from lda.modules.evaluation.domain.release import (
    EligibilityDecision,
    EvaluationRecord,
    ReleaseCriteria,
    verify_run,
)
from lda.modules.evaluation.domain.release_dataset import (
    RELEASE_DATASET,
    RELEASE_GATE_POLICY,
    release_criteria,
)
from lda.modules.evaluation.domain.report import CaseResult, EvaluationReport, MetricResult

__all__ = [
    "CASE_ERROR",
    "CITATION_PRECISION",
    "CITATION_RECALL",
    "CONFIG_ID",
    "ELIGIBILITY_CONFIG_MISMATCH",
    "ELIGIBILITY_DATASET_MISMATCH",
    "ELIGIBILITY_EVIDENCE_FUTURE",
    "ELIGIBILITY_EVIDENCE_STALE",
    "ELIGIBILITY_INCOMPLETE",
    "ELIGIBILITY_INCONSISTENT",
    "ELIGIBILITY_NOT_RECORDED",
    "ELIGIBILITY_NO_EVIDENCE",
    "ELIGIBILITY_POLICY_MISMATCH",
    "ELIGIBILITY_RECENT_FAILURE",
    "ELIGIBILITY_RECORD_CORRUPT",
    "ELIGIBILITY_RUN_FAILED",
    "ELIGIBILITY_SUPERSEDED",
    "GATE_ERROR_RATE_EXCEEDED",
    "GATE_METRIC_BELOW_MINIMUM",
    "GATE_METRIC_NOT_MEASURED",
    "GATE_TOO_FEW_CASES",
    "INVALID_INPUT",
    "ISOLATION",
    "LIMIT_EXCEEDED",
    "OBSERVATION_INVALID",
    "OUTCOME_ACCURACY",
    "PERSISTENCE_FAILED",
    "RECORD_CORRUPT",
    "RELEASE_DATASET",
    "RELEASE_GATE_POLICY",
    "RETRIEVAL_RECALL",
    "RUN_EXISTS",
    "AnsweringIdentity",
    "CaseResult",
    "CheckEligibility",
    "ChunkingIdentity",
    "CitationsIdentity",
    "ConfigComponent",
    "EffectiveConfiguration",
    "EligibilityDecision",
    "EligibilityOutcome",
    "EvaluateConfiguration",
    "EvaluationCase",
    "EvaluationDataset",
    "EvaluationLimits",
    "EvaluationRecord",
    "EvaluationReport",
    "EvaluationRunResult",
    "EvaluationStore",
    "EvaluationTarget",
    "EvidenceSnapshot",
    "GateDecision",
    "GateFailure",
    "GatePolicy",
    "GenerationIdentity",
    "Metric",
    "MetricResult",
    "MetricThreshold",
    "Observation",
    "Outcome",
    "PromptIdentity",
    "RagConfiguration",
    "RecordEligibility",
    "ReleaseCriteria",
    "RetrievalIdentity",
    "RunEvaluation",
    "RunEvaluationCommand",
    "StoredEligibility",
    "StoredRun",
    "default_metrics",
    "release_criteria",
    "verify_run",
]
