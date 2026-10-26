"""The `index` stage: idempotent, fenced, lock-ordered, purge-aware (ADR-008, ADR-010 §4)."""

from __future__ import annotations

import pytest

from lda.modules.ingestion.api import StagePermanentFailure, StageSucceeded
from lda.modules.retrieval.api import STAGE_NAME
from lda.modules.retrieval.domain.failures import (
    CHUNKS_UNAVAILABLE,
    EMBEDDING_INVALID,
    INDEX_CONFLICT,
    VERSION_UNAVAILABLE,
)
from lda.modules.retrieval.tests.support import SECRET, Rig
from lda.shared.kernel.errors import ConflictError, InfrastructureError

TEXTS = [f"{SECRET} applies to every party", "Fees are due monthly in advance", "Notice in writing"]


def test_the_stage_is_named_index() -> None:
    assert STAGE_NAME == "index"


def test_a_chunk_set_is_embedded_and_recorded_with_no_text(rig: Rig) -> None:
    document, version = rig.new_document(TEXTS)
    assert isinstance(rig.index(document, version), StageSucceeded)
    store = rig.retrieval.store
    assert len(store.rows) == 3 and len(store.versions) == 1
    record = next(iter(store.versions.values()))
    assert record.index_id == rig.embedder.identity.index_id
    for row in store.rows.values():
        assert SECRET not in repr(row) and SECRET not in repr(record)
    assert rig.embedder.document_calls == 2  # batch_size=2 over three chunks


def test_a_duplicate_delivery_does_nothing_twice(rig: Rig) -> None:
    document, version = rig.new_document(TEXTS)
    rig.index(document, version)
    before = (dict(rig.retrieval.store.rows), dict(rig.retrieval.store.versions))
    calls = rig.embedder.document_calls
    assert isinstance(rig.index(document, version), StageSucceeded)
    assert (dict(rig.retrieval.store.rows), dict(rig.retrieval.store.versions)) == before
    assert rig.embedder.document_calls == calls  # the recorded version is not embedded again


def test_a_changed_chunk_set_conflicts_instead_of_replacing(rig: Rig) -> None:
    document, version = rig.new_document(TEXTS)
    rig.index(document, version)
    rig.chunks.put(document, version, ["something else entirely"])
    assert rig.index(document, version) == StagePermanentFailure(INDEX_CONFLICT)
    assert len(rig.retrieval.store.rows) == 3


def test_an_unreadable_chunk_set_is_a_permanent_failure(rig: Rig) -> None:
    document, _ = rig.new_document(TEXTS)
    other = rig.new_version(document, TEXTS)
    rig.chunks.forget(document, other)  # chunking has nothing for it
    assert rig.index(document, other) == StagePermanentFailure(CHUNKS_UNAVAILABLE)
    assert not rig.retrieval.store.rows


def test_a_malformed_embedding_is_a_permanent_failure_and_records_nothing(rig: Rig) -> None:
    document, version = rig.new_document(TEXTS)
    rig.embedder.corrupt = True
    assert rig.index(document, version) == StagePermanentFailure(EMBEDDING_INVALID)
    assert not rig.retrieval.store.rows and not rig.retrieval.store.versions


def test_a_failing_embedder_is_retried_not_recorded(rig: Rig) -> None:
    document, version = rig.new_document(TEXTS)
    rig.embedder.fail_with = InfrastructureError("embedder_unavailable")
    with pytest.raises(InfrastructureError):
        rig.index(document, version)
    assert not rig.retrieval.store.rows
    rig.embedder.fail_with = None
    assert isinstance(rig.index(document, version), StageSucceeded)


def test_a_failed_commit_leaves_nothing_and_a_retry_succeeds(rig: Rig) -> None:
    document, version = rig.new_document(TEXTS)
    store = rig.retrieval.store
    store.fail_commit_on = {len(rig.retrieval.begun) + 1}  # the write transaction
    with pytest.raises(InfrastructureError):
        rig.index(document, version)
    assert not store.rows and not store.versions  # atomic: no record without vectors
    store.fail_commit_on = set()
    assert isinstance(rig.index(document, version), StageSucceeded)
    assert len(store.rows) == 3


def test_a_stale_executor_cannot_record_anything(rig: Rig) -> None:
    document, version = rig.new_document(TEXTS)
    rig.retrieval.store.fence_rejects = lambda job, token: token < 5
    with pytest.raises(ConflictError):
        rig.stage.run(rig.request(document, version, token=1))
    assert not rig.retrieval.store.rows
    assert isinstance(rig.stage.run(rig.request(document, version, token=5)), StageSucceeded)


def test_the_write_takes_the_documents_lifecycle_lock(rig: Rig) -> None:
    document, version = rig.new_document(TEXTS)
    rig.index(document, version)
    assert any(document in uow.locked for uow in rig.retrieval.begun)


def test_nothing_is_added_after_the_purge_receipt(rig: Rig) -> None:
    document, version = rig.new_document(TEXTS)
    rig.purge_document(document)  # purge won the race
    assert rig.index(document, version) == StagePermanentFailure(VERSION_UNAVAILABLE)
    assert not rig.retrieval.store.rows


def test_a_late_write_after_the_purge_started_is_refused_or_removed(rig: Rig) -> None:
    """Whichever of stage and purge takes the lock first, no row survives the purge."""
    document, version = rig.new_document(TEXTS)
    rig.index(document, version)  # the stage won: the purge must remove it
    view = rig.purge_document(document)
    assert view.purged and view.versions_purged == 1
    assert not rig.retrieval.store.rows and not rig.retrieval.store.versions
