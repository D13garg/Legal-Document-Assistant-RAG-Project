"""Reading a recorded chunk artifact back into a `ChunkSet` (ADR-038, Step 11 amendment).

The artifact is canonical, so the strongest integrity check is a round trip: the parsed set must
re-serialise to exactly the stored bytes. That also proves every chunk id is the one this module
would mint, so a reader never receives an id this module did not derive.
"""

from __future__ import annotations

import json

from lda.modules.chunking.domain.chunk import SCHEMA_VERSION, Chunk, ChunkSet, Segment
from lda.modules.chunking.domain.record import ChunkSetRecord
from lda.shared.kernel.errors import ValidationError

ARTIFACT_CORRUPT = "chunk_artifact_corrupt"


def chunk_set_from_artifact(artifact: bytes, record: ChunkSetRecord) -> ChunkSet:
    """Parse `artifact` as the chunk set `record` describes, or raise `ValidationError`."""
    try:
        payload = json.loads(artifact.decode("ascii"))
        if (
            payload["schema"] != SCHEMA_VERSION
            or payload["document_id"] != str(record.document_id)
            or payload["version_id"] != str(record.version_id)
        ):
            raise ValidationError(ARTIFACT_CORRUPT)
        chunks = tuple(
            Chunk(
                ordinal=item["ordinal"],
                text=item["text"],
                segments=tuple(Segment(b, s, e) for b, s, e in item["segments"]),
                section_path=tuple(item["section_path"]),
                page_start=item["page_start"],
                page_end=item["page_end"],
            )
            for item in payload["chunks"]
        )
        chunk_set = ChunkSet(
            record.tenant_id,
            record.document_id,
            record.version_id,
            payload["extraction_hash"],
            payload["chunker_version"],
            chunks,
        )
    except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
        raise ValidationError(ARTIFACT_CORRUPT) from None
    if chunk_set.to_bytes() != artifact:
        raise ValidationError(ARTIFACT_CORRUPT)
    return chunk_set
