"""Deterministic orphan recovery (ADR-005) and the job adapters; no manual cleanup is needed."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.documents.application.dto.documents import DocumentReference, ReapUploadCommand
from lda.modules.documents.application.job_handlers import PurgeJobHandler, UploadIntentJobHandler
from lda.modules.documents.application.jobs import PURGE_JOB, UPLOAD_INTENT_JOB
from lda.modules.documents.application.upload_staging import StagedOriginal
from lda.modules.documents.domain.evidence import UploadIntent
from lda.modules.documents.domain.resource import original_path
from lda.modules.documents.testing import InMemoryDocumentsUnitOfWorkFactory
from lda.modules.documents.tests.support import (
    ALICE,
    BODY,
    PDF,
    POLICY,
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    FlakyStore,
    Rig,
    a_document_id,
    build_rig,
    ctx_for,
    uploaded_ids,
)
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import JobId
from lda.shared.ports.jobs import ClaimedJob, Failed, JsonValue, RetryAfter, Succeeded
from lda.shared.ports.object_store import TenantScopedKey

GRACE = timedelta(seconds=POLICY.grace_seconds)
CTX = ctx_for(TENANT_A, ALICE)


def stage_only(rig: Rig) -> StagedOriginal:
    """An upload that crashed after the object landed, before its rows: the orphan scenario."""
    return rig.staging.stage(CTX, a_document_id(9), PDF, BODY)


def command(rig: Rig, staged: StagedOriginal) -> ReapUploadCommand:
    return ReapUploadCommand(a_document_id(9), staged.version_id)


def test_an_orphan_is_found_from_the_database_alone_and_removed_after_the_grace(rig: Rig) -> None:
    staged = stage_only(rig)
    assert rig.store.exists(staged.key)
    early = rig.reap.execute(SYSTEM_A, command(rig, staged))
    assert early.outcome == "not_due" and early.retry_after_seconds == POLICY.grace_seconds
    assert rig.store.exists(staged.key)  # nothing is touched before the grace elapsed
    rig.clock.advance(GRACE - timedelta(seconds=1))
    assert rig.reap.execute(SYSTEM_A, command(rig, staged)).outcome == "not_due"
    rig.clock.advance(timedelta(seconds=1))
    done = rig.reap.execute(SYSTEM_A, command(rig, staged))
    assert done.outcome == "orphan_removed"
    assert not rig.store.exists(staged.key) and rig.factory.store.intents == {}
    assert rig.reap.execute(SYSTEM_A, command(rig, staged)).outcome == "already_clean"


def test_a_stale_intent_of_a_committed_version_never_costs_the_object(rig: Rig) -> None:
    document_id, version_id = uploaded_ids(rig, CTX)
    rig.factory.store.intents[(TENANT_A, version_id)] = UploadIntent(
        TENANT_A, version_id, document_id, rig.clock.now()
    )
    rig.clock.advance(GRACE)
    result = rig.reap.execute(SYSTEM_A, ReapUploadCommand(document_id, version_id))
    assert result.outcome == "version_exists"
    key = TenantScopedKey(TENANT_A, original_path(document_id, version_id))
    assert rig.store.get(key) == BODY and rig.factory.store.intents == {}


def test_the_object_never_reached_the_store_is_still_cleaned_up(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    store.fail_put = True
    with pytest.raises(InfrastructureError):
        rig.staging.stage(CTX, a_document_id(9), PDF, BODY)
    ((_, version_id),) = memory_factory.store.intents
    clock.advance(GRACE)
    result = rig.reap.execute(SYSTEM_A, ReapUploadCommand(a_document_id(9), version_id))
    assert result.outcome == "orphan_removed" and memory_factory.store.intents == {}


def test_an_unverifiable_removal_keeps_the_intent_so_the_job_retries(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    staged = rig.staging.stage(CTX, a_document_id(9), PDF, BODY)
    clock.advance(GRACE)
    store.ignore_delete = True
    with pytest.raises(InfrastructureError) as caught:
        rig.reap.execute(SYSTEM_A, command(rig, staged))
    assert caught.value.code == "orphan_removal_unverified"
    assert len(memory_factory.store.intents) == 1  # still findable
    store.ignore_delete = False
    assert rig.reap.execute(SYSTEM_A, command(rig, staged)).outcome == "orphan_removed"


def test_a_failing_store_keeps_the_intent_and_a_retry_completes(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    staged = rig.staging.stage(CTX, a_document_id(9), PDF, BODY)
    clock.advance(GRACE)
    store.fail_delete = True
    with pytest.raises(InfrastructureError):
        rig.reap.execute(SYSTEM_A, command(rig, staged))
    assert len(memory_factory.store.intents) == 1 and store.exists(staged.key)
    store.fail_delete = False
    assert rig.reap.execute(SYSTEM_A, command(rig, staged)).outcome == "orphan_removed"


def test_the_reaper_is_tenant_scoped(rig: Rig) -> None:
    staged = stage_only(rig)
    rig.clock.advance(GRACE)
    other = rig.reap.execute(type(SYSTEM_A)(TENANT_B, "x"), command(rig, staged))
    assert other.outcome == "already_clean" and rig.store.exists(staged.key)


def test_the_reaper_audits_nothing(rig: Rig) -> None:
    staged = stage_only(rig)
    rig.clock.advance(GRACE)
    before = sum(len(u.audit.facts) for u in rig.factory.begun)
    rig.reap.execute(SYSTEM_A, command(rig, staged))
    assert sum(len(u.audit.facts) for u in rig.factory.begun) == before


# ---- job adapters -------------------------------------------------------------------------------


def job(job_type: str, payload: Mapping[str, JsonValue]) -> ClaimedJob:
    return ClaimedJob(
        id=JobId(UUID(int=1)),
        tenant_id=TENANT_A,
        queue="documents",
        job_type=job_type,
        payload=payload,
        attempt=1,
        max_attempts=5,
        fencing_token=1,
    )


def test_the_intent_job_drives_the_reaper_end_to_end_from_the_enqueued_spec(rig: Rig) -> None:
    handler = UploadIntentJobHandler(rig.reap)
    staged = stage_only(rig)
    (spec,) = [j for u in rig.factory.begun for j in u.jobs.jobs]
    claimed = job(spec.job_type, spec.payload)
    retry = handler.handle(claimed, SYSTEM_A)
    assert isinstance(retry, RetryAfter) and retry.consumes_attempt is False
    assert retry.delay == GRACE and retry.error_code == "upload_intent_not_due"
    rig.clock.advance(GRACE)
    assert handler.handle(claimed, SYSTEM_A) == Succeeded()
    assert not rig.store.exists(staged.key)
    assert handler.handle(claimed, SYSTEM_A) == Succeeded()  # idempotent


def test_the_purge_job_drives_purge_and_is_idempotent(rig: Rig) -> None:
    document_id, version_id = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    (spec,) = [j for u in rig.factory.begun for j in u.jobs.jobs if j.job_type == PURGE_JOB]
    handler = PurgeJobHandler(rig.purge)
    claimed = job(spec.job_type, spec.payload)
    assert handler.handle(claimed, SYSTEM_A) == Succeeded()
    assert (TENANT_A, document_id) in rig.factory.store.receipts
    assert handler.handle(claimed, SYSTEM_A) == Succeeded()
    assert not rig.store.exists(TenantScopedKey(TENANT_A, original_path(document_id, version_id)))


@pytest.mark.parametrize(
    "payload",
    [{}, {"document_id": "not-a-uuid"}, {"document_id": 5}, {"version_id": str(UUID(int=2))}],
)
def test_malformed_payloads_fail_permanently_instead_of_retrying(
    rig: Rig, payload: dict[str, JsonValue]
) -> None:
    purge = PurgeJobHandler(rig.purge).handle(job(PURGE_JOB, payload), SYSTEM_A)
    assert purge == Failed(error_code="job_payload_invalid")
    reap = UploadIntentJobHandler(rig.reap).handle(job(UPLOAD_INTENT_JOB, payload), SYSTEM_A)
    assert reap == Failed(error_code="job_payload_invalid")


def test_handler_failures_propagate_so_the_worker_retries_and_dead_letters(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    document_id, _ = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    store.fail_delete = True
    claimed = job(PURGE_JOB, {"document_id": str(document_id)})
    with pytest.raises(InfrastructureError):
        PurgeJobHandler(rig.purge).handle(claimed, SYSTEM_A)
