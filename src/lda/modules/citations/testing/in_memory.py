"""In-memory twins and fakes for citations tests (never imported by production code)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from lda.modules.citations.application.ports import ChunkContent, ChunkProvenance
from lda.modules.citations.domain.codes import RECORD_EXISTS
from lda.modules.citations.domain.ids import CitedDocumentId as DocumentId
from lda.modules.citations.domain.ids import CitedVersionId as VersionId
from lda.modules.citations.domain.record import GenerationRecord
from lda.shared.kernel.errors import AuthorizationError, ConflictError, NotFoundError
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.authorization import ProofExpectation


class InMemoryGenerationRecords:
    """Insert-only, tenant-bound; mirrors the repository contract."""

    def __init__(self) -> None:
        self._rows: dict[tuple[TenantId, UUID], GenerationRecord] = {}

    def add(self, record: GenerationRecord) -> None:
        key = (record.tenant_id, record.record_id)
        if key in self._rows:
            raise ConflictError(RECORD_EXISTS)
        self._rows[key] = record

    def get(self, tenant_id: TenantId, record_id: UUID) -> GenerationRecord | None:
        return self._rows.get((tenant_id, record_id))


class FakeProvenance:
    """Chunk provenance per `(tenant, document, version)`; anything else is unavailable."""

    def __init__(self) -> None:
        self.versions: dict[
            tuple[TenantId, DocumentId, VersionId], tuple[ChunkProvenance, ...]
        ] = {}
        self.reads = 0

    def put(
        self,
        tenant_id: TenantId,
        document_id: DocumentId,
        version_id: VersionId,
        chunks: tuple[ChunkProvenance, ...],
    ) -> None:
        self.versions[(tenant_id, document_id, version_id)] = chunks

    def drop(self, tenant_id: TenantId, document_id: DocumentId, version_id: VersionId) -> None:
        self.versions.pop((tenant_id, document_id, version_id), None)

    def read(
        self, tenant_id: TenantId, document_id: DocumentId, version_id: VersionId
    ) -> tuple[ChunkProvenance, ...]:
        self.reads += 1
        found = self.versions.get((tenant_id, document_id, version_id))
        if found is None:
            raise NotFoundError("chunks_not_found")
        return found


class FakeContent:
    """Chunk text per `(tenant, document, version)`; anything else is unavailable. Counts reads."""

    def __init__(self) -> None:
        self.versions: dict[tuple[TenantId, DocumentId, VersionId], tuple[ChunkContent, ...]] = {}
        self.reads = 0

    def put(
        self,
        tenant_id: TenantId,
        document_id: DocumentId,
        version_id: VersionId,
        chunks: tuple[ChunkContent, ...],
    ) -> None:
        self.versions[(tenant_id, document_id, version_id)] = chunks

    def read(
        self, tenant_id: TenantId, document_id: DocumentId, version_id: VersionId
    ) -> tuple[ChunkContent, ...]:
        self.reads += 1
        found = self.versions.get((tenant_id, document_id, version_id))
        if found is None:
            raise NotFoundError("chunks_not_found")
        return found


class RecordingVerifier:
    """A `ProofVerifier` that accepts one token for exactly one expectation, and logs calls."""

    def __init__(self, token: str, accepted: ProofExpectation | None = None) -> None:
        self.token = token
        self.accepted = accepted
        self.calls: list[tuple[str, ProofExpectation]] = []
        self.revoke_after: int | None = None

    def verify(self, token: str, expected: ProofExpectation) -> None:
        self.calls.append((token, expected))
        revoked = self.revoke_after is not None and len(self.calls) > self.revoke_after
        if token != self.token or revoked or (self.accepted and expected != self.accepted):
            raise AuthorizationError("proof_invalid")


class FixedClock:
    def __init__(self, moment: datetime | None = None) -> None:
        self.moment = moment or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.moment


class FreshIds:
    def new_uuid(self) -> UUID:
        return uuid4()
