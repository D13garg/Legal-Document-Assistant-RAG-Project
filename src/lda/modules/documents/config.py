"""The `documents` configuration slice (ADR-031, ADR-005). Every field has a safe default."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_MEDIA_TYPES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
)
MIN_GRACE_SECONDS = 300  # must exceed the object store client's request timeouts (ADR-005)


class DocumentsSettings(BaseModel):
    """`LDA_DOCUMENTS__MAX_UPLOAD_BYTES`, `__ALLOWED_MEDIA_TYPES` (JSON list) and
    `__UPLOAD_INTENT_GRACE_SECONDS`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    allowed_media_types: tuple[str, ...] = DEFAULT_MEDIA_TYPES
    upload_intent_grace_seconds: int = Field(default=900, ge=MIN_GRACE_SECONDS)

    @field_validator("allowed_media_types")
    @classmethod
    def _non_empty_lowercase(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(v != v.lower() or "/" not in v for v in value):
            raise ValueError(
                "allowed_media_types must be a non-empty list of lowercase media types"
            )
        return value
