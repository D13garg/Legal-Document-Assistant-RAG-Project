"""The resource a cited source stands for, in the forms authorization uses (ADR-014, ADR-016).

A citation is not an authorization. Before a cited source is shown or its text read again, the
caller needs a fresh decision covering `source_resource(source)`.
"""

from __future__ import annotations

from lda.modules.access_control.api import ResourceRef
from lda.modules.citations.domain.ids import CitedDocumentId
from lda.modules.citations.domain.source import DOCUMENT_KIND, CitationSource
from lda.shared.ports.authorization import ProofResource


def _ref(document_id: CitedDocumentId) -> ResourceRef:
    return ResourceRef(DOCUMENT_KIND, str(document_id.value))  # validates the kind and id shape


def source_resource(source: CitationSource) -> ResourceRef:
    return _ref(source.document_id)


def proof_resource(document_id: CitedDocumentId) -> ProofResource:
    ref = _ref(document_id)
    return ProofResource(ref.kind, ref.id)
