"""Run-and-record, record-eligibility and check-eligibility (ADR-041 D3, D4).

Global, tenant-less and synthetic: no tenant, principal, document, question or answer is involved,
so there is nothing to authorize or audit per tenant; the insert-only tables are the record. All
three take an `EffectiveConfiguration` (the typed identity), never a bare id, so an unverified
identifier cannot reach a decision.

Eligibility means: for this exact configuration, the newest stored run on the approved dataset
verified (hash, canonical text, configuration, dataset, policy, age, completeness, consistency,
passing) and no failing run on that configuration and dataset is younger than the maximum age.
It deploys nothing.
"""

from __future__ import annotations

from lda.modules.evaluation.application.dto import RunEvaluationCommand
from lda.modules.evaluation.application.release_dto import EligibilityOutcome, EvaluationRunResult
from lda.modules.evaluation.application.release_ports import (
    EvaluationStore,
    EvidenceSnapshot,
    StoredRun,
)
from lda.modules.evaluation.application.use_cases.run_evaluation import RunEvaluation
from lda.modules.evaluation.domain import codes
from lda.modules.evaluation.domain.dataset import EvaluationDataset
from lda.modules.evaluation.domain.effective_configuration import EffectiveConfiguration
from lda.modules.evaluation.domain.release import (
    EligibilityDecision,
    EvaluationRecord,
    ReleaseCriteria,
    verify_run,
)
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import ValidationError
from lda.shared.ports.clock import Clock, IdGenerator


def _epoch(clock: Clock) -> int:
    return int(clock.now().timestamp())


@public(reason="Global synthetic evaluation; no tenant, principal or customer data.")
@not_audited(reason="Global synthetic data; the insert-only evaluation_runs table is the record.")
class EvaluateConfiguration:
    """Run the approved dataset against the target for one configuration and store the result,
    pass or fail. A run that cannot be stored is an error and returns no result."""

    def __init__(
        self,
        runner: RunEvaluation,
        dataset: EvaluationDataset,
        criteria: ReleaseCriteria,
        store: EvaluationStore,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        if (
            ReleaseCriteria.from_dataset(dataset, criteria.policy, criteria.max_age_seconds)
            != criteria
        ):
            raise ValidationError(codes.INVALID_INPUT, detail="criteria")  # dataset != criteria
        self._runner, self._dataset, self._criteria = runner, dataset, criteria
        self._store, self._clock, self._ids = store, clock, ids

    def execute(self, effective: EffectiveConfiguration) -> EvaluationRunResult:
        if not isinstance(effective, EffectiveConfiguration):
            raise ValidationError(codes.INVALID_INPUT, detail="configuration")
        configuration = effective.configuration()
        report = self._runner.execute(
            RunEvaluationCommand(self._dataset, configuration, self._criteria.policy)
        )
        record = EvaluationRecord(
            str(self._ids.new_uuid()),
            _epoch(self._clock),
            configuration,
            self._dataset.dataset_id,
            self._dataset.version,
            self._dataset.fingerprint(),
            self._criteria.policy,
            report,
        )
        stored = self._store.append_run(record)
        return EvaluationRunResult(
            stored.run_id,
            record.config_id,
            record.passed,
            stored.record_sha256,
            tuple((f.code, f.subject) for f in report.decision.failures),
        )


def _verify(
    store: EvaluationStore,
    criteria: ReleaseCriteria,
    effective: EffectiveConfiguration,
    now: int,
) -> tuple[StoredRun | None, list[str], EvidenceSnapshot]:
    snapshot = store.snapshot(
        effective.config_id,
        criteria.dataset_id,
        criteria.dataset_version,
        now - criteria.max_age_seconds,
    )
    run = snapshot.latest_run
    if run is None:
        return None, [codes.ELIGIBILITY_NO_EVIDENCE], snapshot
    _, failures = verify_run(run.record_text, run.record_sha256, criteria, effective.config_id, now)
    if snapshot.failed_since > 0 and codes.ELIGIBILITY_RUN_FAILED not in failures:
        failures.append(codes.ELIGIBILITY_RECENT_FAILURE)
    return run, failures, snapshot


@public(reason="Global synthetic evaluation; no tenant, principal or customer data.")
@not_audited(
    reason="Global synthetic data; the insert-only release_eligibility table is the record."
)
class RecordEligibility:
    """Verify the newest run and, only if it is valid evidence now, append an eligibility record
    naming that exact run. Otherwise nothing is written."""

    def __init__(
        self, criteria: ReleaseCriteria, store: EvaluationStore, clock: Clock, ids: IdGenerator
    ) -> None:
        self._criteria, self._store, self._clock, self._ids = criteria, store, clock, ids

    def execute(self, effective: EffectiveConfiguration) -> EligibilityOutcome:
        if not isinstance(effective, EffectiveConfiguration):
            raise ValidationError(codes.INVALID_INPUT, detail="configuration")
        now = _epoch(self._clock)
        run, failures, _ = _verify(self._store, self._criteria, effective, now)
        decision = EligibilityDecision.of(failures)
        if run is None or not decision.eligible:
            return EligibilityOutcome(decision, None, False)
        stored, replayed = self._store.record_eligibility(str(self._ids.new_uuid()), run, now)
        return EligibilityOutcome(decision, stored.eligibility_id, replayed)


@public(reason="Global synthetic evaluation; no tenant, principal or customer data.")
@not_audited(reason="Read-only query over global synthetic data; it changes no state.")
class CheckEligibility:
    """Is this exact configuration eligible right now? Requires an eligibility record, that the
    record names the newest run, and that the run still verifies as valid evidence at the
    current time. A storage failure raises; a caller must treat a raise as not eligible."""

    def __init__(self, criteria: ReleaseCriteria, store: EvaluationStore, clock: Clock) -> None:
        self._criteria, self._store, self._clock = criteria, store, clock

    def execute(self, effective: EffectiveConfiguration) -> EligibilityDecision:
        if not isinstance(effective, EffectiveConfiguration):
            raise ValidationError(codes.INVALID_INPUT, detail="configuration")
        now = _epoch(self._clock)
        run, failures, snapshot = _verify(self._store, self._criteria, effective, now)
        entry = snapshot.latest_eligibility
        if run is not None:
            if entry is None:
                failures.append(codes.ELIGIBILITY_NOT_RECORDED)
            elif (
                entry.run_id != run.run_id
                or entry.run_sha256 != run.record_sha256
                or entry.config_id != effective.config_id
            ):
                failures.append(codes.ELIGIBILITY_SUPERSEDED)
        return EligibilityDecision.of(failures)
