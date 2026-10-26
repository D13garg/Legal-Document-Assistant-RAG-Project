"""The extracted, normalized, structured text of one document version.

This is the derived artifact (ADR-037). It is immutable and canonical: the same blocks always give
the same bytes and therefore the same hash, which is what makes a re-run after a crash provably
idempotent. It is content: it lives only in the object store, never in Postgres, an event or a log.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from lda.shared.kernel.errors import ValidationError

SCHEMA_VERSION = 1
MAX_HEADING_LEVEL = 9


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    """Resource limits (ADR-037). `max_pages` applies to formats that have pages."""

    max_bytes: int
    max_pages: int
    max_characters: int
    max_expanded_bytes: int

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_pages, self.max_characters, self.max_expanded_bytes) < 1:
            raise ValidationError("extraction_limits_invalid")


@dataclass(frozen=True, slots=True)
class Block:
    """One unit of text in reading order. `level` is set exactly for headings; `page` (1-based)
    only for formats that have pages."""

    kind: BlockKind
    text: str
    level: int | None = None
    page: int | None = None

    def __post_init__(self) -> None:
        if not self.text or self.text != self.text.strip() or "\n" in self.text:
            raise ValidationError("extraction_block_text_invalid")
        if (self.kind is BlockKind.HEADING) != (self.level is not None):
            raise ValidationError("extraction_block_level_invalid")
        if self.level is not None and not 1 <= self.level <= MAX_HEADING_LEVEL:
            raise ValidationError("extraction_block_level_invalid")
        if self.page is not None and self.page < 1:
            raise ValidationError("extraction_block_page_invalid")


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    media_type: str
    blocks: tuple[Block, ...]
    page_count: int | None = None

    def __post_init__(self) -> None:
        if not self.blocks:
            raise ValidationError("extraction_empty")
        if self.page_count is not None and self.page_count < 1:
            raise ValidationError("extraction_page_count_invalid")
        for block in self.blocks:
            if block.page is not None and (self.page_count is None or block.page > self.page_count):
                raise ValidationError("extraction_block_page_invalid")

    @property
    def character_count(self) -> int:
        return sum(len(block.text) for block in self.blocks)

    def to_bytes(self) -> bytes:
        """Canonical JSON: sorted keys, no whitespace, ASCII only."""
        payload: dict[str, object] = {
            "schema": SCHEMA_VERSION,
            "media_type": self.media_type,
            "page_count": self.page_count,
            "blocks": [
                {"kind": b.kind.value, "text": b.text, "level": b.level, "page": b.page}
                for b in self.blocks
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")

    @classmethod
    def from_bytes(cls, data: bytes) -> ExtractedDocument:
        try:
            payload = json.loads(data.decode("ascii"))
            if payload["schema"] != SCHEMA_VERSION:
                raise ValidationError("extraction_schema_unsupported")
            return cls(
                media_type=payload["media_type"],
                page_count=payload["page_count"],
                blocks=tuple(
                    Block(BlockKind(b["kind"]), b["text"], b["level"], b["page"])
                    for b in payload["blocks"]
                ),
            )
        except ValidationError:
            raise
        except (ValueError, KeyError, TypeError):
            raise ValidationError("extraction_artifact_invalid") from None


def content_hash(artifact: bytes) -> str:
    return hashlib.sha256(artifact).hexdigest()
