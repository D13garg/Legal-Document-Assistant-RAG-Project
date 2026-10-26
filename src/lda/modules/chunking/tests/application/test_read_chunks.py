"""`ReadChunks`: the only chunk read path (ADR-038 amendment, ADR-008)."""

from __future__ import annotations

import pytest

from lda.modules.chunking.api import (
    CHUNKS_ARTIFACT_CORRUPT,
    CHUNKS_ARTIFACT_MISSING,
    CHUNKS_NOT_FOUND,
    CHUNKS_VERSION_NOT_ELIGIBLE,
    DocumentId,
    PurgeCommand,
    ReadChunks,
    ReadChunksCommand,
    VersionId,
)
from lda.modules.chunking.tests.support import SECRET, SYSTEM_A, TENANT_B, Rig
from lda.modules.documents.api import ReadVersionState, VersionStatus
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ConflictError, NotFoundError
from lda.shared.ports.object_store import TenantScopedKey

BODY = f"# Terms\n\n{SECRET} applies to every party.\n\n## Fees\n\nFees are due monthly in advance."


@pytest.fixture
def reader(rig: Rig) -> ReadChunks:
    return ReadChunks(rig.chunking, rig.objects, ReadVersionState(rig.documents))


def _chunked(rig: Rig) -> tuple[DocumentId, VersionId]:
    document, version = rig.upload_document(BODY)
    assert rig.stage.run(rig.request(document, version)).__class__.__name__ == "StageSucceeded"
    return document, version


def test_returns_ordered_chunks_with_stable_ids_and_text(rig: Rig, reader: ReadChunks) -> None:
    document, version = _chunked(rig)
    first = reader.execute(SYSTEM_A, ReadChunksCommand(document, version))
    again = reader.execute(SYSTEM_A, ReadChunksCommand(document, version))
    assert first == again  # deterministic: ids and text never change between reads
    assert [c.ordinal for c in first.chunks] == list(range(len(first.chunks)))
    assert len({c.chunk_id for c in first.chunks}) == len(first.chunks)
    assert SECRET in "".join(c.text for c in first.chunks)
    assert (first.document_id, first.version_id) == (document, version)


def test_the_result_never_shows_text_in_a_repr(rig: Rig, reader: ReadChunks) -> None:
    document, version = _chunked(rig)
    stored = reader.execute(SYSTEM_A, ReadChunksCommand(document, version))
    assert SECRET not in repr(stored) and SECRET not in repr(stored.chunks[0])


@pytest.mark.parametrize("status", [VersionStatus.FAILED, VersionStatus.QUARANTINED])
def test_a_failed_or_quarantined_version_is_refused(
    rig: Rig, reader: ReadChunks, status: VersionStatus
) -> None:
    document, version = _chunked(rig)
    rig.mark(document, version, status)
    with pytest.raises(ConflictError) as excinfo:
        reader.execute(SYSTEM_A, ReadChunksCommand(document, version))
    assert excinfo.value.code == CHUNKS_VERSION_NOT_ELIGIBLE


def test_a_ready_version_is_served(rig: Rig, reader: ReadChunks) -> None:
    document, version = _chunked(rig)
    rig.mark(document, version, VersionStatus.READY)
    assert reader.execute(SYSTEM_A, ReadChunksCommand(document, version)).chunks


def test_a_tombstoned_document_serves_nothing_even_before_the_purge(
    rig: Rig, reader: ReadChunks
) -> None:
    document, version = _chunked(rig)
    rig.tombstone(document)
    with pytest.raises(NotFoundError):
        reader.execute(SYSTEM_A, ReadChunksCommand(document, version))


def test_another_tenant_sees_nothing(rig: Rig, reader: ReadChunks) -> None:
    document, version = _chunked(rig)
    with pytest.raises(NotFoundError):
        reader.execute(
            SystemContext(TENANT_B, "chunking-test"), ReadChunksCommand(document, version)
        )


def test_a_version_without_a_chunk_set_is_not_found(rig: Rig, reader: ReadChunks) -> None:
    document, version = rig.upload_document(BODY)  # extracted, never chunked
    with pytest.raises(NotFoundError) as excinfo:
        reader.execute(SYSTEM_A, ReadChunksCommand(document, version))
    assert excinfo.value.code == CHUNKS_NOT_FOUND


def test_a_missing_artifact_is_reported_without_a_key(rig: Rig, reader: ReadChunks) -> None:
    document, version = _chunked(rig)
    rig.objects.delete(rig.key(document, version))
    with pytest.raises(NotFoundError) as excinfo:
        reader.execute(SYSTEM_A, ReadChunksCommand(document, version))
    assert excinfo.value.code == CHUNKS_ARTIFACT_MISSING
    assert str(document) not in str(excinfo.value)


def test_a_tampered_artifact_is_never_served(rig: Rig, reader: ReadChunks) -> None:
    document, version = _chunked(rig)
    key: TenantScopedKey = rig.key(document, version)
    original = rig.objects.get(key)
    rig.objects.put(
        key,
        original.replace(b"applies", b"APPLIES"),
        content_type="application/json",
        overwrite=True,
    )
    with pytest.raises(ConflictError) as excinfo:
        reader.execute(SYSTEM_A, ReadChunksCommand(document, version))
    assert excinfo.value.code == CHUNKS_ARTIFACT_CORRUPT


def test_a_consistent_but_non_canonical_artifact_is_refused(rig: Rig, reader: ReadChunks) -> None:
    """Even with a matching recorded hash the set must round-trip: ids are never taken on trust."""
    document, version = _chunked(rig)
    key = rig.key(document, version)
    forged = rig.objects.get(key).replace(b'"ordinal":0,', b'"ordinal":0, ', 1)  # whitespace
    record = rig.store.chunk_sets[(SYSTEM_A.tenant_id, version)]
    import hashlib
    from dataclasses import replace

    rig.store.chunk_sets[(SYSTEM_A.tenant_id, version)] = replace(
        record, content_hash=hashlib.sha256(forged).hexdigest(), artifact_bytes=len(forged)
    )
    rig.objects.put(key, forged, content_type="application/json", overwrite=True)
    with pytest.raises(ConflictError) as excinfo:
        reader.execute(SYSTEM_A, ReadChunksCommand(document, version))
    assert excinfo.value.code == CHUNKS_ARTIFACT_CORRUPT


def test_nothing_is_served_after_the_purge(rig: Rig, reader: ReadChunks) -> None:
    document, version = _chunked(rig)
    rig.tombstone(document)
    rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    with pytest.raises(NotFoundError):
        reader.execute(SYSTEM_A, ReadChunksCommand(document, version))
