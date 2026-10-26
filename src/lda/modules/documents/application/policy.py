from __future__ import annotations

from dataclasses import dataclass

from lda.shared.kernel.errors import ValidationError


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    """Limits from the `documents` configuration slice (ADR-005), checked before any write."""

    max_bytes: int
    allowed_media_types: frozenset[str]
    grace_seconds: int

    def check(self, media_type: str, size_bytes: int) -> None:
        if media_type not in self.allowed_media_types:
            raise ValidationError("document_media_type_not_allowed")
        if size_bytes < 1:
            raise ValidationError("document_empty")
        if size_bytes > self.max_bytes:
            raise ValidationError("document_too_large")
