"""PDF text layer via pypdf. No OCR: a PDF without a text layer is a permanent failure.

pypdf runs in this process, so the limits below cannot guarantee a hard CPU or memory bound against
a hostile file (ADR-037, known limitation). Encrypted PDFs, including those with only an owner
password, are refused: decrypting them needs a crypto dependency the project does not have.
"""

from __future__ import annotations

import io

from pypdf import PdfReader

from lda.modules.document_processing.domain.extraction import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionLimits,
)
from lda.modules.document_processing.domain.failures import (
    EMPTY_DOCUMENT,
    ENCRYPTED_DOCUMENT,
    LIMIT_CHARACTERS,
    LIMIT_PAGES,
    MALFORMED_DOCUMENT,
    NO_EXTRACTABLE_TEXT,
    ExtractionRejected,
)
from lda.modules.document_processing.domain.text import paragraphs

PDF = "application/pdf"


def parse_pdf(content: bytes, limits: ExtractionLimits) -> ExtractedDocument:
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise ExtractionRejected(ENCRYPTED_DOCUMENT)
        page_count = len(reader.pages)
    except ExtractionRejected:
        raise
    except Exception:  # pypdf raises many unrelated types for damaged input
        raise ExtractionRejected(MALFORMED_DOCUMENT) from None
    if page_count == 0:
        raise ExtractionRejected(EMPTY_DOCUMENT)
    if page_count > limits.max_pages:
        raise ExtractionRejected(LIMIT_PAGES)  # decided before any page is parsed
    blocks: list[Block] = []
    total = 0
    for number in range(page_count):
        try:
            raw = reader.pages[number].extract_text()
        except Exception:
            raise ExtractionRejected(MALFORMED_DOCUMENT) from None
        for paragraph in paragraphs(raw):
            total += len(paragraph)
            if total > limits.max_characters:
                raise ExtractionRejected(LIMIT_CHARACTERS)
            blocks.append(Block(BlockKind.PARAGRAPH, paragraph, page=number + 1))
    if not blocks:
        raise ExtractionRejected(NO_EXTRACTABLE_TEXT)
    return ExtractedDocument(PDF, tuple(blocks), page_count=page_count)
