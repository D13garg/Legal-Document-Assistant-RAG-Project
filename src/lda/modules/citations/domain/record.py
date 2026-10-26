"""`GenerationRecord`: the immutable provenance of one AI output (invariant 5, ADR-016).

Identifiers, hashes and references only: no prompt, no answer text, no excerpt, no question and no
authorization proof. Frozen, and invalid combinations cannot be built: an answered generation
carries at least one citation; a non-answer carries none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from lda.modules.citations.domain.codes import INVALID_INPUT, RECORD_INVALID
from lda.modules.citations.domain.evidence import Citation
from lda.modules.citations.domain.source import digest
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId

_CONFIG_ID = re.compile(r"rag-[0-9a-f]{64}")  # the ADR-030 shape; evaluation is not imported
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
MAX_RECORD_CITATIONS = 500


class GenerationOutcome(StrEnum):
    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REJECTED = "rejected"
    """The output failed validation and was not shown."""


def _label(value: object, field: str) -> str:
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None:
        raise ValidationError(INVALID_INPUT, detail=field)
    return value


@dataclass(frozen=True, slots=True)
class GenerationRecord:
    record_id: UUID
    tenant_id: TenantId
    principal_id: PrincipalId
    request_id: RequestId
    created_at: datetime
    config_id: str
    outcome: GenerationOutcome
    template_id: str
    template_version: str
    model_id: str
    request_sha256: str
    evidence_fingerprint: str
    citations: tuple[Citation, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.record_id, UUID)
            or not isinstance(self.tenant_id, TenantId)
            or not isinstance(self.principal_id, PrincipalId)
            or not isinstance(self.request_id, RequestId)
            or not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
            or not isinstance(self.outcome, GenerationOutcome)
        ):
            raise ValidationError(INVALID_INPUT, detail="record")
        if not isinstance(self.config_id, str) or _CONFIG_ID.fullmatch(self.config_id) is None:
            raise ValidationError(INVALID_INPUT, detail="config_id")
        for name in ("template_id", "template_version", "model_id"):
            _label(getattr(self, name), name)
        digest(self.request_sha256, "request_sha256")
        digest(self.evidence_fingerprint, "evidence_fingerprint")
        if not isinstance(self.citations, tuple) or len(self.citations) > MAX_RECORD_CITATIONS:
            raise ValidationError(INVALID_INPUT, detail="citations")
        markers = [c.marker for c in self.citations if isinstance(c, Citation)]
        answered = self.outcome is GenerationOutcome.ANSWERED
        if (
            len(markers) != len(self.citations)
            or len(set(markers)) != len(markers)
            or answered != bool(self.citations)
        ):
            raise ValidationError(RECORD_INVALID, detail="citations")
