"""Search request and result, and the purge command (ADR-008).

`SearchResult` is a ranked list of references with provenance. It is NOT an authorization proof and
NOT an authorized context: it carries no text, and building prompt context from it needs a fresh
authorization decision in the module that builds the context (ADR-014, ADR-017 decision 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lda.modules.access_control.api import AccessScope
from lda.modules.retrieval.domain.ids import ChunkId, DocumentId, VersionId


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """`scope` and `proof_token` come from ONE Access Control decision for `documents.read_document`
    over exactly `scope.resources`; the proof is verified before anything else happens."""

    scope: AccessScope
    proof_token: str = field(repr=False)
    text: str = field(repr=False)
    limit: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    rank: int
    document_id: DocumentId
    version_id: VersionId
    version_number: int
    chunk_id: ChunkId
    ordinal: int
    score: float
    text_hash: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    index_id: str
    hits: tuple[SearchHit, ...]


@dataclass(frozen=True, slots=True)
class PurgeCommand:
    document_id: DocumentId


@dataclass(frozen=True, slots=True)
class PurgeView:
    document_id: DocumentId
    purged: bool
    versions_purged: int
