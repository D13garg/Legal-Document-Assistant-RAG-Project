"""Public surface of chunking (deterministic chunk sets of extracted text; ADR-038).

Bootstrap composes `ChunkStage` into the ingestion plan, after `extract`, and registers the purge
handler. The chunk artifact and its key layout stay private to this module. `ReadChunks` (added in
Step 11, ADR-008) is the only way another module obtains chunk text and chunk ids, for a system
caller, after Documents confirmed the version is eligible.
"""

from lda.modules.chunking.application.chunk_stage import STAGE_NAME, ChunkStage
from lda.modules.chunking.application.dto import PurgeCommand, PurgeView
from lda.modules.chunking.application.event_handlers import PurgeChunksOnDocumentPurged
from lda.modules.chunking.application.read_dto import ChunkView, ReadChunksCommand, StoredChunks
from lda.modules.chunking.application.use_cases.purge_chunks import PurgeChunks
from lda.modules.chunking.application.use_cases.read_chunks import (
    CHUNKS_ARTIFACT_CORRUPT,
    CHUNKS_ARTIFACT_MISSING,
    CHUNKS_NOT_FOUND,
    CHUNKS_VERSION_NOT_ELIGIBLE,
    ReadChunks,
)
from lda.modules.chunking.domain.chunker import ChunkingParameters
from lda.modules.chunking.domain.failures import PERMANENT_CODES, ChunkingRejected
from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.modules.chunking.domain.record import PURGE_SCOPE

__all__ = [
    "CHUNKS_ARTIFACT_CORRUPT",
    "CHUNKS_ARTIFACT_MISSING",
    "CHUNKS_NOT_FOUND",
    "CHUNKS_VERSION_NOT_ELIGIBLE",
    "PERMANENT_CODES",
    "PURGE_SCOPE",
    "STAGE_NAME",
    "ChunkStage",
    "ChunkView",
    "ChunkingParameters",
    "ChunkingRejected",
    "DocumentId",
    "PurgeChunks",
    "PurgeChunksOnDocumentPurged",
    "PurgeCommand",
    "PurgeView",
    "ReadChunks",
    "ReadChunksCommand",
    "StoredChunks",
    "VersionId",
]
