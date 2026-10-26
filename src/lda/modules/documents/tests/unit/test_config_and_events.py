from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError as PydanticError

from lda.modules.documents.application.policy import UploadPolicy
from lda.modules.documents.config import DEFAULT_MEDIA_TYPES, MIN_GRACE_SECONDS, DocumentsSettings
from lda.modules.documents.domain.events import (
    DocumentDeleted,
    DocumentPurged,
    DocumentVersionAdded,
)
from lda.modules.documents.domain.ids import DocumentVersionId
from lda.modules.documents.tests.support import POLICY, TENANT_A, a_document_id
from lda.platform.events.codec import EventCodec
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import EventId


def test_defaults_are_the_approved_limits() -> None:
    settings = DocumentsSettings()
    assert settings.max_upload_bytes == 50 * 1024 * 1024
    assert settings.allowed_media_types == DEFAULT_MEDIA_TYPES
    assert settings.upload_intent_grace_seconds == 900
    assert "application/pdf" in DEFAULT_MEDIA_TYPES and "text/plain" in DEFAULT_MEDIA_TYPES


@pytest.mark.parametrize(
    "bad",
    [
        {"max_upload_bytes": 0},
        {"upload_intent_grace_seconds": MIN_GRACE_SECONDS - 1},
        {"allowed_media_types": ()},
        {"allowed_media_types": ("Application/PDF",)},
        {"allowed_media_types": ("pdf",)},
        {"unknown_field": 1},
    ],
)
def test_invalid_settings_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(PydanticError):
        DocumentsSettings(**bad)  # type: ignore[arg-type]


def test_the_minimum_grace_boundary_is_accepted() -> None:
    assert DocumentsSettings(upload_intent_grace_seconds=300).upload_intent_grace_seconds == 300


def test_the_policy_checks_media_type_then_size() -> None:
    POLICY.check("application/pdf", 1)
    POLICY.check("application/pdf", 1000)
    for media, size, code in [
        ("image/png", 10, "document_media_type_not_allowed"),
        ("application/pdf", 0, "document_empty"),
        ("application/pdf", 1001, "document_too_large"),
    ]:
        with pytest.raises(ValidationError) as caught:
            POLICY.check(media, size)
        assert caught.value.code == code
    assert isinstance(POLICY, UploadPolicy)


def test_events_round_trip_through_the_platform_codec_and_carry_identifiers_only() -> None:
    codec = EventCodec()
    for event_type in (DocumentVersionAdded, DocumentDeleted, DocumentPurged):
        codec.register(event_type)
    when, document_id = datetime(2026, 1, 1, tzinfo=UTC), a_document_id()
    events: list[DomainEvent] = [
        DocumentVersionAdded(
            event_id=EventId(UUID(int=1)),
            tenant_id=TENANT_A,
            occurred_at=when,
            document_id=document_id,
            version_id=DocumentVersionId(UUID(int=7)),
            version_number=2,
        ),
        DocumentDeleted(
            event_id=EventId(UUID(int=2)),
            tenant_id=TENANT_A,
            occurred_at=when,
            document_id=document_id,
        ),
        DocumentPurged(
            event_id=EventId(UUID(int=3)),
            tenant_id=TENANT_A,
            occurred_at=when,
            document_id=document_id,
            versions_purged=3,
        ),
    ]
    for event in events:
        assert codec.decode(codec.encode(event)) == event
        assert not {"title", "media_type", "body", "content"} & {
            f.name for f in dataclasses.fields(event)
        }
    assert {e.type_name for e in events} == {
        "documents.DocumentVersionAdded",
        "documents.DocumentDeleted",
        "documents.DocumentPurged",
    }
