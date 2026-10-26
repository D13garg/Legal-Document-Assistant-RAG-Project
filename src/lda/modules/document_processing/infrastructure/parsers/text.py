from __future__ import annotations

import codecs

from lda.modules.document_processing.domain.extraction import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionLimits,
)
from lda.modules.document_processing.domain.failures import (
    EMPTY_DOCUMENT,
    LIMIT_CHARACTERS,
    MALFORMED_DOCUMENT,
    ExtractionRejected,
)
from lda.modules.document_processing.domain.text import paragraphs

TEXT_PLAIN = "text/plain"


def _decode(content: bytes) -> str:
    """UTF-8 (with or without a BOM) or UTF-16 with a BOM. No other encoding is guessed."""
    try:
        if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return content.decode("utf-16")
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ExtractionRejected(MALFORMED_DOCUMENT) from None


def parse_text(content: bytes, limits: ExtractionLimits) -> ExtractedDocument:
    text = _decode(content)
    if "\x00" in text:
        raise ExtractionRejected(MALFORMED_DOCUMENT)  # binary data, not text
    blocks: list[Block] = []
    total = 0
    for paragraph in paragraphs(text):
        total += len(paragraph)
        if total > limits.max_characters:
            raise ExtractionRejected(LIMIT_CHARACTERS)
        blocks.append(Block(BlockKind.PARAGRAPH, paragraph))
    if not blocks:
        raise ExtractionRejected(EMPTY_DOCUMENT)
    return ExtractedDocument(TEXT_PLAIN, tuple(blocks))
