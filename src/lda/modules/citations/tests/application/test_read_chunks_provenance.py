"""The ReadChunks adapter keeps provenance and drops text; read failures pass through unchanged."""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from lda.modules.chunking.api import (
    ChunkView,
    DocumentId,
    ReadChunksCommand,
    StoredChunks,
    VersionId,
)
from lda.modules.citations.api import (
    ChunkProvenance,
    CitedDocumentId,
    CitedVersionId,
    ReadChunksProvenance,
)
from lda.modules.citations.application.provenance import PURPOSE
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ConflictError
from lda.shared.kernel.ids import TenantId

SECRET_TEXT = "PRIVILEGED CLIENT TEXT"  # noqa: S105 - a marker string for a leak check


class FakeReadChunks:
    def __init__(self, document: DocumentId, version: VersionId) -> None:
        self.document, self.version = document, version
        self.seen: list[tuple[SystemContext, ReadChunksCommand]] = []
        self.view = ChunkView(uuid4(), 0, SECRET_TEXT, "a" * 64, ("Secret heading",), 3, 4)
        self.fail = False

    def execute(self, ctx: SystemContext, command: ReadChunksCommand) -> StoredChunks:
        self.seen.append((ctx, command))
        if self.fail:
            raise ConflictError("chunks_version_not_eligible")
        return StoredChunks(self.document, self.version, "b" * 64, "v1", "c" * 64, (self.view,))


def adapter() -> tuple[ReadChunksProvenance, FakeReadChunks, DocumentId, VersionId]:
    doc, ver = DocumentId(uuid4()), VersionId(uuid4())
    fake = FakeReadChunks(doc, ver)
    return ReadChunksProvenance(fake), fake, doc, ver  # type: ignore[arg-type]


def test_only_identifiers_hashes_and_pages_come_back() -> None:
    reader, fake, doc, ver = adapter()
    tenant = TenantId(uuid4())
    (found,) = reader.read(tenant, CitedDocumentId(doc.value), CitedVersionId(ver.value))
    assert found == ChunkProvenance(fake.view.chunk_id, 0, "a" * 64, 3, 4)
    assert SECRET_TEXT not in repr(found) and "Secret heading" not in repr(found)
    ctx, command = fake.seen[0]
    assert (ctx.tenant_id, ctx.purpose) == (tenant, PURPOSE)
    assert (command.document_id, command.version_id) == (doc, ver)


def test_unavailable_versions_propagate_for_the_builder_to_map() -> None:
    reader, fake, doc, ver = adapter()
    fake.fail = True
    with pytest.raises(ConflictError):
        reader.read(TenantId(uuid4()), CitedDocumentId(doc.value), CitedVersionId(ver.value))


def test_chunk_provenance_has_no_text_field() -> None:
    assert {f.name for f in dataclasses.fields(ChunkProvenance)} == {
        "chunk_id",
        "ordinal",
        "text_hash",
        "page_start",
        "page_end",
    }


def test_the_content_adapter_keeps_the_text_through_the_same_read_chunks_path() -> None:
    from lda.modules.citations.api import ChunkContent, ReadChunksContent
    from lda.modules.citations.application.content import PURPOSE as CONTENT_PURPOSE

    doc, ver = DocumentId(uuid4()), VersionId(uuid4())
    fake = FakeReadChunks(doc, ver)
    reader = ReadChunksContent(fake)  # type: ignore[arg-type]
    tenant = TenantId(uuid4())
    (found,) = reader.read(tenant, CitedDocumentId(doc.value), CitedVersionId(ver.value))
    assert found == ChunkContent(fake.view.chunk_id, 0, "a" * 64, 3, 4, SECRET_TEXT)
    assert SECRET_TEXT not in repr(found) and "Secret heading" not in repr(found)
    ctx, _ = fake.seen[0]
    assert (ctx.tenant_id, ctx.purpose) == (tenant, CONTENT_PURPOSE)
    assert CONTENT_PURPOSE != PURPOSE  # reads are attributable to the evidence path


def test_the_content_adapter_passes_unavailable_versions_through() -> None:
    from lda.modules.citations.api import ReadChunksContent

    doc, ver = DocumentId(uuid4()), VersionId(uuid4())
    fake = FakeReadChunks(doc, ver)
    fake.fail = True
    with pytest.raises(ConflictError):
        ReadChunksContent(fake).read(  # type: ignore[arg-type]
            TenantId(uuid4()), CitedDocumentId(doc.value), CitedVersionId(ver.value)
        )
