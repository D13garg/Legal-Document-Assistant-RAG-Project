"""`ChunkContentReader` that serves chunk text of `ready` versions only (ADR-029 section 1 (d)).

Why it exists. `ReadChunks` serves `pending` as well as `ready` versions, because chunking's own
pipeline stages (`index`) read a version before it can become `ready`. Retrieval only ever returns
`ready` versions, and ADR-015 section 4 says a `pending` version is never searchable. Generation
sends chunk text to a provider, so it must not rely on its callers citing only what retrieval
returned: the claims are caller-supplied references (ADR-016). This reader asks Documents, which
owns the lifecycle, before the text store is touched, and refuses everything that is not `ready`.

Refusals are the single `citations_source_unavailable` (never a lifecycle-specific code), exactly
like a purged, foreign-tenant or `failed`/`quarantined` version, so a caller cannot probe which
state a version is in. `citations`' builder discards the cause. It composes AFTER authorization:
`TextEvidenceBuilder` verifies the proof before it calls any reader.

Scope, stated plainly: this enforces *eligibility* (`ready`), not *currency*. An older `ready`
version of a document is still a `ready` version; choosing the current one is retrieval's default
scope (ADR-008 section 5), not an access rule (ADR-029 limitations).
"""

from __future__ import annotations

from typing import Protocol

from lda.modules.citations.api import (
    SOURCE_UNAVAILABLE,
    ChunkContent,
    ChunkContentReader,
    CitedDocumentId,
    CitedVersionId,
)
from lda.modules.documents.api import (
    DocumentId,
    DocumentVersionId,
    VersionStateQuery,
    VersionStateView,
    VersionStatus,
)
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import NotFoundError
from lda.shared.kernel.ids import TenantId

PURPOSE = "grounded_generation.check_version_ready"


class VersionStateReader(Protocol):
    """The shape of `documents.api.ReadVersionState` that this adapter needs."""

    def execute(self, ctx: SystemContext, query: VersionStateQuery) -> VersionStateView: ...


class ReadyVersionContent:
    """Wraps another `ChunkContentReader`; the inner reader is never called for a version that
    Documents does not report as `ready` (or does not know, or has tombstoned)."""

    def __init__(self, versions: VersionStateReader, inner: ChunkContentReader) -> None:
        self._versions = versions
        self._inner = inner

    def read(
        self, tenant_id: TenantId, document_id: CitedDocumentId, version_id: CitedVersionId
    ) -> tuple[ChunkContent, ...]:
        state = self._versions.execute(  # NotFoundError for tombstoned, unknown or foreign
            SystemContext(tenant_id, PURPOSE),
            VersionStateQuery(DocumentId(document_id.value), DocumentVersionId(version_id.value)),
        )
        answered_for_the_asked_version = (state.document_id.value, state.version_id.value) == (
            document_id.value,
            version_id.value,
        )
        if not answered_for_the_asked_version or state.status is not VersionStatus.READY:
            raise NotFoundError(SOURCE_UNAVAILABLE)
        return self._inner.read(tenant_id, document_id, version_id)
