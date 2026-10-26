"""The `document_processing` configuration slice (ADR-031, ADR-037). Safe defaults throughout."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DocumentProcessingSettings(BaseModel):
    """`LDA_DOCUMENT_PROCESSING__MAX_BYTES`, `__MAX_PAGES`, `__MAX_CHARACTERS` and
    `__MAX_EXPANDED_BYTES`.

    The defaults match Documents' upload limit (50 MiB). `max_expanded_bytes` bounds what one
    compressed DOCX part may decompress to (a zip-bomb guard). These limits are checked by the
    parsers; they do not bound CPU time or memory of a hostile PDF (ADR-037).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    max_pages: int = Field(default=1000, ge=1)
    max_characters: int = Field(default=10_000_000, ge=1)
    max_expanded_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
