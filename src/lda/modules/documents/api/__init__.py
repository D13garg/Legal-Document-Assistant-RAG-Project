"""Public surface of documents (ownership, immutable versions, originals, tombstone and purge)."""

from lda.modules.documents.application.dto.documents import (
    AddDocumentVersionCommand,
    DeleteView,
    DocumentReference,
    DocumentView,
    OriginalContent,
    OutcomeResult,
    OutcomeView,
    PurgeVerificationView,
    PurgeView,
    ReadOriginalCommand,
    ReapUploadCommand,
    ReapView,
    RecordOutcomeCommand,
    UploadDocumentCommand,
    UploadedView,
    VersionStateQuery,
    VersionStateView,
    VersionView,
)
from lda.modules.documents.application.event_handlers import (
    CancelPipelineOnDocumentDeleted,
    ClearPipelineRecordsOnPurge,
    RecordFailedOnFailed,
    RecordQuarantinedOnQuarantined,
    RecordReadyOnCompleted,
    StartPipelineOnVersionAdded,
)
from lda.modules.documents.application.job_handlers import PurgeJobHandler, UploadIntentJobHandler
from lda.modules.documents.application.jobs import DOCUMENTS_QUEUE, PURGE_JOB, UPLOAD_INTENT_JOB
from lda.modules.documents.application.policy import UploadPolicy
from lda.modules.documents.application.queries.get_document import GetDocument
from lda.modules.documents.application.queries.read_original import (
    ORIGINAL_MISSING,
    ReadOriginal,
)
from lda.modules.documents.application.queries.read_version_state import ReadVersionState
from lda.modules.documents.application.queries.verify_document_purge import VerifyDocumentPurge
from lda.modules.documents.application.upload_staging import UploadStaging
from lda.modules.documents.application.use_cases.add_document_version import AddDocumentVersion
from lda.modules.documents.application.use_cases.delete_document import DeleteDocument
from lda.modules.documents.application.use_cases.purge_document import PurgeDocument
from lda.modules.documents.application.use_cases.reap_abandoned_upload import ReapAbandonedUpload
from lda.modules.documents.application.use_cases.record_version_outcome import RecordVersionOutcome
from lda.modules.documents.application.use_cases.upload_document import UploadDocument
from lda.modules.documents.domain.document import DocumentStatus, VersionStatus
from lda.modules.documents.domain.events import (
    DocumentDeleted,
    DocumentPurged,
    DocumentVersionAdded,
)
from lda.modules.documents.domain.evidence import PURGE_SCOPE
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.modules.documents.domain.resource import (
    ADD_DOCUMENT_VERSION,
    DELETE_DOCUMENT,
    DOCUMENT_ACTIONS,
    DOCUMENT_KIND,
    READ_DOCUMENT,
)

__all__ = [
    "ADD_DOCUMENT_VERSION",
    "DELETE_DOCUMENT",
    "DOCUMENTS_QUEUE",
    "DOCUMENT_ACTIONS",
    "DOCUMENT_KIND",
    "ORIGINAL_MISSING",
    "PURGE_JOB",
    "PURGE_SCOPE",
    "READ_DOCUMENT",
    "UPLOAD_INTENT_JOB",
    "AddDocumentVersion",
    "AddDocumentVersionCommand",
    "CancelPipelineOnDocumentDeleted",
    "ClearPipelineRecordsOnPurge",
    "DeleteDocument",
    "DeleteView",
    "DocumentDeleted",
    "DocumentId",
    "DocumentPurged",
    "DocumentReference",
    "DocumentStatus",
    "DocumentVersionAdded",
    "DocumentVersionId",
    "DocumentView",
    "GetDocument",
    "OriginalContent",
    "OutcomeResult",
    "OutcomeView",
    "PurgeDocument",
    "PurgeJobHandler",
    "PurgeVerificationView",
    "PurgeView",
    "ReadOriginal",
    "ReadOriginalCommand",
    "ReadVersionState",
    "ReapAbandonedUpload",
    "ReapUploadCommand",
    "ReapView",
    "RecordFailedOnFailed",
    "RecordOutcomeCommand",
    "RecordQuarantinedOnQuarantined",
    "RecordReadyOnCompleted",
    "RecordVersionOutcome",
    "StartPipelineOnVersionAdded",
    "UploadDocument",
    "UploadDocumentCommand",
    "UploadIntentJobHandler",
    "UploadPolicy",
    "UploadStaging",
    "UploadedView",
    "VerifyDocumentPurge",
    "VersionStateQuery",
    "VersionStateView",
    "VersionStatus",
    "VersionView",
]
