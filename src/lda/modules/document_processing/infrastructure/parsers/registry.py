from __future__ import annotations

import defusedxml
import pypdf

from lda.modules.document_processing.domain.extraction import ExtractedDocument, ExtractionLimits
from lda.modules.document_processing.domain.failures import (
    EMPTY_DOCUMENT,
    LIMIT_BYTES,
    UNSUPPORTED_MEDIA_TYPE,
    ExtractionRejected,
)
from lda.modules.document_processing.infrastructure.parsers.docx import DOCX, parse_docx
from lda.modules.document_processing.infrastructure.parsers.pdf import PDF, parse_pdf
from lda.modules.document_processing.infrastructure.parsers.text import TEXT_PLAIN, parse_text

EXTRACTION_SCHEMA = "extract-1"
PARSER_VERSION = (
    f"{EXTRACTION_SCHEMA}; pypdf={pypdf.__version__}; defusedxml={defusedxml.__version__}"
)


class FormatParsers:
    """Chooses the parser by the media type Documents recorded (never by sniffing the content) and
    applies the limits every format shares before any parsing starts."""

    @property
    def version(self) -> str:
        return PARSER_VERSION

    def parse(self, media_type: str, content: bytes, limits: ExtractionLimits) -> ExtractedDocument:
        if media_type not in (PDF, DOCX, TEXT_PLAIN):
            raise ExtractionRejected(UNSUPPORTED_MEDIA_TYPE)
        if not content:
            raise ExtractionRejected(EMPTY_DOCUMENT)
        if len(content) > limits.max_bytes:
            raise ExtractionRejected(LIMIT_BYTES)
        if media_type == PDF:
            return parse_pdf(content, limits)
        if media_type == DOCX:
            return parse_docx(content, limits)
        return parse_text(content, limits)
