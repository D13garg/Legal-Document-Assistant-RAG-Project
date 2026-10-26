"""Public surface of document_processing (extraction of document text; ADR-037).

Bootstrap composes `ExtractStage` into the ingestion plan and registers the purge handler. Later
modules (chunking) read the derived artifact through `ReadExtraction`, which asks Documents first;
the key layout stays private to this module.
"""

from lda.modules.document_processing.application.dto import (
    PurgeCommand,
    PurgeView,
    ReadExtractionCommand,
    StoredExtraction,
)
from lda.modules.document_processing.application.event_handlers import (
    PurgeExtractionsOnDocumentPurged,
)
from lda.modules.document_processing.application.extract_stage import STAGE_NAME, ExtractStage
from lda.modules.document_processing.application.ports import ContentParser
from lda.modules.document_processing.application.queries.read_extraction import (
    EXTRACTION_ARTIFACT_CORRUPT,
    EXTRACTION_ARTIFACT_MISSING,
    EXTRACTION_NOT_FOUND,
    EXTRACTION_VERSION_NOT_ELIGIBLE,
    ReadExtraction,
)
from lda.modules.document_processing.application.use_cases.purge_extractions import (
    PurgeExtractions,
)
from lda.modules.document_processing.domain.extraction import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionLimits,
)
from lda.modules.document_processing.domain.failures import PERMANENT_CODES, ExtractionRejected
from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.modules.document_processing.domain.record import PURGE_SCOPE

__all__ = [
    "EXTRACTION_ARTIFACT_CORRUPT",
    "EXTRACTION_ARTIFACT_MISSING",
    "EXTRACTION_NOT_FOUND",
    "EXTRACTION_VERSION_NOT_ELIGIBLE",
    "PERMANENT_CODES",
    "PURGE_SCOPE",
    "STAGE_NAME",
    "Block",
    "BlockKind",
    "ContentParser",
    "DocumentId",
    "ExtractStage",
    "ExtractedDocument",
    "ExtractionLimits",
    "ExtractionRejected",
    "PurgeCommand",
    "PurgeExtractions",
    "PurgeExtractionsOnDocumentPurged",
    "PurgeView",
    "ReadExtraction",
    "ReadExtractionCommand",
    "StoredExtraction",
    "VersionId",
]
