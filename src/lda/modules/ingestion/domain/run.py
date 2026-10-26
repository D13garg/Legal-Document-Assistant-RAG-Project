"""The ingestion run: the state machine that decides what `completed` means (ADR-010).

A run is `running` until exactly one of: every stage of its frozen plan succeeded (`completed`), a
stage failed permanently (`failed`), the scan found malware (`quarantined`), retries ran out or the
run stalled (`dead_lettered`), or its document was deleted (`cancelled`). Only `dead_lettered` can
still be left (by cancelling). Database triggers repeat these rules.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from lda.modules.ingestion.domain.errors import InvalidRun, InvalidTransition, valid_error_code
from lda.modules.ingestion.domain.ids import DocumentRef, IngestionRunId, VersionRef
from lda.shared.kernel.ids import TenantId

STAGE_NAME_PATTERN = r"[a-z][a-z0-9_]{0,63}"
_STAGE_NAME = re.compile(STAGE_NAME_PATTERN)
_RUN_NAMESPACE = uuid.UUID("5d3f3d54-8f0a-4b62-9a0c-6a3b1d1c7e11")


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    QUARANTINED = "quarantined"


# Runs whose result Documents must learn about (ADR-010 §7).
OUTCOME_STATES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.QUARANTINED})


def run_id_for(tenant_id: TenantId, version: VersionRef, generation: int) -> IngestionRunId:
    """Deterministic: the same (tenant, version, generation) is always the same run."""
    name = f"{tenant_id.value}:{version.value}:{generation}"
    return IngestionRunId(uuid.uuid5(_RUN_NAMESPACE, name))


def valid_stage_name(name: str) -> str:
    if not isinstance(name, str) or _STAGE_NAME.fullmatch(name) is None:
        raise InvalidRun("ingestion_stage_name_invalid")
    return name


def _milliseconds(start: datetime | None, end: datetime) -> int:
    return 0 if start is None else max(0, int((end - start).total_seconds() * 1000))


@dataclass
class StageRun:
    name: str
    position: int
    status: StageStatus = StageStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error_code: str | None = None


@dataclass
class IngestionRun:
    id: IngestionRunId
    tenant_id: TenantId
    document: DocumentRef
    version: VersionRef
    generation: int
    status: RunStatus
    stages: list[StageRun]
    created_at: datetime
    progress_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    acknowledged_at: datetime | None = None
    redeliveries: int = 0

    @classmethod
    def start(
        cls,
        tenant_id: TenantId,
        document: DocumentRef,
        version: VersionRef,
        generation: int,
        stage_names: tuple[str, ...],
        now: datetime,
    ) -> IngestionRun:
        if generation < 1:
            raise InvalidRun("ingestion_generation_invalid")
        if not stage_names or len(set(stage_names)) != len(stage_names):
            raise InvalidRun("ingestion_plan_invalid")
        stages = [StageRun(valid_stage_name(n), i) for i, n in enumerate(stage_names)]
        return cls(
            run_id_for(tenant_id, version, generation),
            tenant_id,
            document,
            version,
            generation,
            RunStatus.RUNNING,
            stages,
            now,
            now,
        )

    # ---- queries -------------------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.status is RunStatus.RUNNING

    @property
    def has_outcome(self) -> bool:
        return self.status in OUTCOME_STATES

    def stage(self, name: str) -> StageRun:
        for candidate in self.stages:
            if candidate.name == name:
                return candidate
        raise InvalidTransition

    def next_stage(self) -> StageRun | None:
        """The first stage that has not succeeded; stages run strictly in plan order."""
        if self.status is not RunStatus.RUNNING:
            return None
        return next((s for s in self.stages if s.status is not StageStatus.SUCCEEDED), None)

    # ---- stage transitions ---------------------------------------------------------------------

    def begin_stage(self, name: str, now: datetime) -> None:
        stage = self._expected(name)
        if stage.status not in (StageStatus.PENDING, StageStatus.RUNNING):
            raise InvalidTransition  # RUNNING again = a previous executor died mid-stage
        stage.status = StageStatus.RUNNING
        stage.attempts += 1
        stage.started_at = now
        stage.finished_at = None
        stage.duration_ms = None
        self.progress_at = now

    def succeed_stage(self, name: str, now: datetime) -> None:
        stage = self._running(name)
        self._finish(stage, StageStatus.SUCCEEDED, None, now)

    def retry_stage(self, name: str, error_code: str, now: datetime) -> None:
        stage = self._running(name)
        code = valid_error_code(error_code)  # validate BEFORE any state changes
        stage.status = StageStatus.PENDING
        stage.error_code = code
        stage.finished_at = None
        self.progress_at = now

    def fail_stage(self, name: str, error_code: str, now: datetime) -> None:
        stage = self._running(name)
        self._finish(stage, StageStatus.FAILED, error_code, now)
        self._close(RunStatus.FAILED, stage.error_code, now)

    def quarantine_stage(self, name: str, error_code: str, now: datetime) -> None:
        stage = self._running(name)
        self._finish(stage, StageStatus.QUARANTINED, error_code, now)
        self._close(RunStatus.QUARANTINED, stage.error_code, now)

    # ---- run transitions -----------------------------------------------------------------------

    def complete(self, now: datetime) -> None:
        """The Step 6 completion condition: every stage of the frozen plan succeeded."""
        if self.status is not RunStatus.RUNNING:
            raise InvalidTransition
        if any(s.status is not StageStatus.SUCCEEDED for s in self.stages):
            raise InvalidTransition
        self._close(RunStatus.COMPLETED, None, now)

    def dead_letter(self, error_code: str, now: datetime) -> None:
        if self.status is not RunStatus.RUNNING:
            raise InvalidTransition
        self._close(RunStatus.DEAD_LETTERED, valid_error_code(error_code), now)

    def cancel(self, now: datetime) -> None:
        if self.status not in (RunStatus.RUNNING, RunStatus.DEAD_LETTERED):
            raise InvalidTransition
        self._close(RunStatus.CANCELLED, self.error_code, now)

    # ---- outcome delivery ----------------------------------------------------------------------

    def acknowledge(self, now: datetime) -> bool:
        """Documents applied or refused the outcome. True when this call recorded it."""
        if not self.has_outcome:
            raise InvalidTransition
        if self.acknowledged_at is not None:
            return False
        self.acknowledged_at = now
        return True

    def record_redelivery(self, now: datetime) -> None:
        if not self.has_outcome or self.acknowledged_at is not None:
            raise InvalidTransition
        self.redeliveries += 1
        self.progress_at = now

    # ---- internals -----------------------------------------------------------------------------

    def _expected(self, name: str) -> StageRun:
        if self.status is not RunStatus.RUNNING:
            raise InvalidTransition
        stage = self.stage(name)
        if self.next_stage() is not stage:
            raise InvalidTransition  # out of order
        return stage

    def _running(self, name: str) -> StageRun:
        stage = self._expected(name)
        if stage.status is not StageStatus.RUNNING:
            raise InvalidTransition
        return stage

    def _finish(
        self, stage: StageRun, status: StageStatus, error_code: str | None, now: datetime
    ) -> None:
        code = None if error_code is None else valid_error_code(error_code)
        stage.status = status
        stage.error_code = code
        stage.finished_at = now
        stage.duration_ms = _milliseconds(stage.started_at, now)
        self.progress_at = now

    def _close(self, status: RunStatus, error_code: str | None, now: datetime) -> None:
        self.status = status
        self.error_code = error_code
        self.finished_at = now
        self.progress_at = now
