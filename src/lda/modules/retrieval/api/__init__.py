"""Public surface of retrieval (vector index projection and authorized search; ADR-004/007/008/009).

Bootstrap composes `IndexStage` into the ingestion plan after `chunk`, and registers the purge
handler. `SearchIndex` returns ranked references with provenance: no text, no prompt, no proof. A
`SearchResult` is NOT an authorized context; whoever builds context from it must authorize again.
"""

from lda.modules.retrieval.application.development_embedder import DevelopmentEmbedder
from lda.modules.retrieval.application.dto import (
    PurgeCommand,
    PurgeView,
    SearchHit,
    SearchQuery,
    SearchResult,
)
from lda.modules.retrieval.application.event_handlers import PurgeRetrievalIndexOnDocumentPurged
from lda.modules.retrieval.application.index_stage import STAGE_NAME, IndexStage
from lda.modules.retrieval.application.limits import SearchLimits
from lda.modules.retrieval.application.ports import (
    ChunkSource,
    Embedder,
    VersionStates,
)
from lda.modules.retrieval.application.source import SourceChunk, SourceChunks
from lda.modules.retrieval.application.use_cases.purge_index import PurgeRetrievalIndex
from lda.modules.retrieval.application.use_cases.search_index import (
    SEARCH_LIMIT_INVALID,
    SEARCH_QUERY_INVALID,
    SEARCH_REQUIRES_PRINCIPAL,
    SEARCH_SCOPE_EMPTY,
    SEARCH_SCOPE_INVALID,
    SEARCH_SCOPE_MISMATCH,
    SearchIndex,
)
from lda.modules.retrieval.domain.embedding import EMBEDDING_DIMENSIONS, EmbeddingIdentity
from lda.modules.retrieval.domain.failures import PERMANENT_CODES, RetrievalRejected
from lda.modules.retrieval.domain.record import PURGE_SCOPE

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "PERMANENT_CODES",
    "PURGE_SCOPE",
    "SEARCH_LIMIT_INVALID",
    "SEARCH_QUERY_INVALID",
    "SEARCH_REQUIRES_PRINCIPAL",
    "SEARCH_SCOPE_EMPTY",
    "SEARCH_SCOPE_INVALID",
    "SEARCH_SCOPE_MISMATCH",
    "STAGE_NAME",
    "ChunkSource",
    "DevelopmentEmbedder",
    "Embedder",
    "EmbeddingIdentity",
    "IndexStage",
    "PurgeCommand",
    "PurgeRetrievalIndex",
    "PurgeRetrievalIndexOnDocumentPurged",
    "PurgeView",
    "RetrievalRejected",
    "SearchHit",
    "SearchIndex",
    "SearchLimits",
    "SearchQuery",
    "SearchResult",
    "SourceChunk",
    "SourceChunks",
    "VersionStates",
]
