from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.ingestion.application.dto.runs import (
    ExecuteRunCommand,
    ExecutionOutcome,
    ExecutionResult,
    WatchdogCommand,
    WatchdogResult,
)
from lda.modules.ingestion.application.errors import RetryRequested
from lda.modules.ingestion.application.job_handlers import RunJobHandler, WatchdogJobHandler
from lda.modules.ingestion.domain.ids import IngestionRunId
from lda.modules.ingestion.tests.support import SYSTEM_A
from lda.shared.kernel.ids import JobId, TenantId
from lda.shared.ports.jobs import ClaimedJob, Failed, RetryAfter, Succeeded

D, V = UUID(int=0xD), UUID(int=0xE)


def job(payload: dict[str, object], *, attempt: int = 2, token: int = 7) -> ClaimedJob:
    return ClaimedJob(
        id=JobId(UUID(int=5)),
        tenant_id=TenantId(UUID(int=0xA1)),
        queue="ingestion",
        job_type="x",
        payload=payload,  # type: ignore[arg-type]
        attempt=attempt,
        max_attempts=4,
        fencing_token=token,
    )


class RecordingExecute:
    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        self.calls: list[tuple[object, ExecuteRunCommand]] = []

    def execute(self, ctx: object, command: ExecuteRunCommand) -> ExecutionResult:
        self.calls.append((ctx, command))
        return self.result


def run_handler(result: ExecutionResult) -> tuple[RunJobHandler, RecordingExecute]:
    fake = RecordingExecute(result)
    return RunJobHandler(fake), fake  # type: ignore[arg-type]


GOOD = {"document_id": str(D), "version_id": str(V), "generation": 3}


def test_the_run_handler_passes_ids_generation_lease_and_attempts_to_the_use_case() -> None:
    handler, fake = run_handler(ExecutionResult(ExecutionOutcome.FINISHED))
    assert handler.handle(job(GOOD), SYSTEM_A) == Succeeded()
    ((ctx, cmd),) = fake.calls
    assert ctx is SYSTEM_A
    assert (cmd.document.value, cmd.version.value, cmd.generation) == (D, V, 3)
    assert (cmd.job_id, cmd.fencing_token, cmd.attempt, cmd.max_attempts) == (
        JobId(UUID(int=5)),
        7,
        2,
        4,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"document_id": str(D), "version_id": str(V)},
        {"document_id": "nope", "version_id": str(V), "generation": 1},
        {"document_id": str(D), "version_id": "nope", "generation": 1},
        {"document_id": str(D), "version_id": str(V), "generation": 0},
        {"document_id": str(D), "version_id": str(V), "generation": "1"},
        {"document_id": str(D), "version_id": str(V), "generation": True},
        {"document_id": str(D), "version_id": str(V), "generation": 1.5},
    ],
)
def test_a_malformed_payload_fails_permanently_without_calling_the_use_case(
    payload: dict[str, object],
) -> None:
    handler, fake = run_handler(ExecutionResult(ExecutionOutcome.FINISHED))
    assert handler.handle(job(payload), SYSTEM_A) == Failed(error_code="job_payload_invalid")
    assert fake.calls == []


def test_inactive_and_finished_runs_complete_the_job() -> None:
    for outcome in (ExecutionOutcome.INACTIVE, ExecutionOutcome.FINISHED):
        handler, _ = run_handler(ExecutionResult(outcome))
        assert handler.handle(job(GOOD), SYSTEM_A) == Succeeded()


def test_a_retry_is_raised_with_its_code_so_the_worker_applies_backoff() -> None:
    handler, _ = run_handler(ExecutionResult(ExecutionOutcome.RETRY, "scan_error"))
    with pytest.raises(RetryRequested) as caught:
        handler.handle(job(GOOD), SYSTEM_A)
    assert caught.value.code == "scan_error"


def test_a_dead_lettered_run_fails_the_job_with_the_run_code() -> None:
    handler, _ = run_handler(ExecutionResult(ExecutionOutcome.DEAD_LETTERED, "scan_error"))
    assert handler.handle(job(GOOD), SYSTEM_A) == Failed(error_code="scan_error")


class RecordingWatchdog:
    def __init__(self, result: WatchdogResult) -> None:
        self.result = result
        self.calls: list[WatchdogCommand] = []

    def execute(self, ctx: object, command: WatchdogCommand) -> WatchdogResult:
        self.calls.append(command)
        return self.result


def test_the_watchdog_handler_reschedules_without_consuming_an_attempt() -> None:
    fake = RecordingWatchdog(WatchdogResult(done=False, next_check_seconds=90))
    outcome = WatchdogJobHandler(fake).handle(job({"run_id": str(UUID(int=9))}), SYSTEM_A)  # type: ignore[arg-type]
    assert outcome == RetryAfter(
        delay=timedelta(seconds=90),
        error_code="ingestion_watchdog_pending",
        consumes_attempt=False,
    )
    (call,) = fake.calls
    assert call.run_id == IngestionRunId(UUID(int=9)) and call.fencing_token == 7


def test_the_watchdog_handler_finishes_when_done_and_rejects_bad_payloads() -> None:
    done = WatchdogJobHandler(RecordingWatchdog(WatchdogResult(done=True)))  # type: ignore[arg-type]
    assert done.handle(job({"run_id": str(UUID(int=9))}), SYSTEM_A) == Succeeded()
    for payload in ({}, {"run_id": "nope"}):
        fake = RecordingWatchdog(WatchdogResult(done=True))
        assert WatchdogJobHandler(fake).handle(job(payload), SYSTEM_A) == Failed(  # type: ignore[arg-type]
            error_code="job_payload_invalid"
        )
        assert fake.calls == []
