"""The validated shape of one audit record (ADR-019 D6).

Built from primitives and kernel types only: the domain does not import `shared/ports`, so the
infrastructure adapter maps an `AuditFact` into an `AuditEntry` and this value decides whether it
is acceptable. Invariant 13: identifiers and labels only, never content.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from lda.modules.audit.domain.errors import InvalidAuditEntry
from lda.shared.kernel.declarations import IDENTIFIER_PATTERN, MAX_IDENTIFIER_LENGTH
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId

OUTCOMES = frozenset({"success", "denied", "failure"})  # a test keeps this equal to AuditOutcome
MAX_ATTRIBUTES = 16
MAX_ATTRIBUTE_KEY_LENGTH = 64
MAX_ATTRIBUTE_VALUE_LENGTH = 256
RESOURCE_TYPE_PATTERN = r"[a-z][a-z0-9_]{0,63}"  # same shape as an access_control resource kind
RESOURCE_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"  # same shape as a resource id
RESERVED_PURPOSE_KEY = "purpose"  # SystemContext.purpose is recorded here (ADR-019 D6)

# A guard against the obvious mistake, not a proof: a key containing one of these words (split on
# `_` and `.`) is refused, so content, secrets and personal data cannot be named into a fact.
FORBIDDEN_KEY_WORDS = frozenset(
    {
        "content",
        "text",
        "body",
        "prompt",
        "completion",
        "response",
        "output",
        "answer",
        "excerpt",
        "snippet",
        "chunk",
        "payload",
        "token",
        "secret",
        "password",
        "credential",
        "key",
        "signature",
        "proof",
        "mac",
        "display",
        "email",
    }
)

_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)
_RESOURCE_TYPE = re.compile(RESOURCE_TYPE_PATTERN)
_RESOURCE_ID = re.compile(RESOURCE_ID_PATTERN)
_WORD_SPLIT = re.compile(r"[_.]")


def _check_attributes(attributes: Mapping[str, str]) -> None:
    if len(attributes) > MAX_ATTRIBUTES:
        raise InvalidAuditEntry("audit_attributes_too_many")
    for key, value in attributes.items():
        if (
            not isinstance(key, str)
            or len(key) > MAX_ATTRIBUTE_KEY_LENGTH
            or _IDENTIFIER.fullmatch(key) is None
        ):
            raise InvalidAuditEntry("audit_attribute_key_invalid")
        if FORBIDDEN_KEY_WORDS.intersection(_WORD_SPLIT.split(key)):
            raise InvalidAuditEntry("audit_attribute_key_forbidden")
        if (
            not isinstance(value, str)
            or len(value) > MAX_ATTRIBUTE_VALUE_LENGTH
            or not value.isprintable()
        ):
            raise InvalidAuditEntry("audit_attribute_value_invalid")


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """What happened, to what, by whom, inside one tenant. Immutable once accepted."""

    tenant_id: TenantId
    actor: PrincipalId | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    request_id: RequestId | None
    occurred_at: datetime
    attributes: Mapping[str, str]

    def __post_init__(self) -> None:
        if len(self.action) > MAX_IDENTIFIER_LENGTH or _IDENTIFIER.fullmatch(self.action) is None:
            raise InvalidAuditEntry("audit_action_invalid")
        if _RESOURCE_TYPE.fullmatch(self.resource_type) is None:
            raise InvalidAuditEntry("audit_resource_type_invalid")
        if self.resource_id is not None and _RESOURCE_ID.fullmatch(self.resource_id) is None:
            raise InvalidAuditEntry("audit_resource_id_invalid")
        if self.outcome not in OUTCOMES:
            raise InvalidAuditEntry("audit_outcome_invalid")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise InvalidAuditEntry("audit_time_naive")
        _check_attributes(self.attributes)
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def require_tenant(self, tenant_id: TenantId) -> None:
        """A fact for another tenant than the unit of work's is rejected (fail-closed)."""
        if self.tenant_id != tenant_id:
            raise InvalidAuditEntry("audit_tenant_mismatch")
