from __future__ import annotations

import copy
import json
import pickle
from datetime import UTC, datetime
from uuid import UUID

import pytest

from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError, LdaError, NotFoundError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import EventId, PrincipalId, TenantId
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.object_store import TenantScopedKey
from lda.shared.ports.rate_limiter import RateLimitKey, RateLimitPolicy

U = UUID(int=1)


def test_ids_are_distinct_types() -> None:
    assert TenantId(U) != PrincipalId(U)  # type: ignore[comparison-overlap]  # mypy also rejects it
    assert TenantId.parse(str(U)) == TenantId(U)
    assert str(TenantId(U)) == str(U)


def test_system_context_requires_purpose() -> None:
    with pytest.raises(ValueError, match="purpose"):
        SystemContext(tenant_id=TenantId(U), purpose=" ")


def test_sensitive_never_leaks() -> None:
    s = Sensitive("secret-text")
    for rendered in (repr(s), str(s), f"{s}", f"{s!r}"):
        assert "secret-text" not in rendered
    assert s.reveal() == "secret-text"
    with pytest.raises(TypeError):
        pickle.dumps(s)
    with pytest.raises(TypeError):
        json.dumps({"x": s})
    assert copy.deepcopy(s) is s
    with pytest.raises(AttributeError):
        s.other = 1


def test_domain_event_requires_aware_time() -> None:
    with pytest.raises(ValueError, match="timezone"):
        DomainEvent(event_id=EventId(U), tenant_id=TenantId(U), occurred_at=datetime(2026, 1, 1))
    DomainEvent(
        event_id=EventId(U), tenant_id=TenantId(U), occurred_at=datetime(2026, 1, 1, tzinfo=UTC)
    )


def test_errors_expose_code_not_detail() -> None:
    err = NotFoundError("document_not_found", detail="internal info")
    assert str(err) == "document_not_found" and err.detail == "internal info"
    assert isinstance(ConflictError("x"), LdaError) and isinstance(
        InfrastructureError("x"), LdaError
    )


@pytest.mark.parametrize("path", ["../x", "a//b", "/abs", "a/../b", "bad path", ""])
def test_object_keys_reject_traversal(path: str) -> None:
    with pytest.raises(ValueError):
        TenantScopedKey(TenantId(U), path)


def test_object_key_is_tenant_prefixed() -> None:
    assert (
        TenantScopedKey(TenantId(U), "documents/d1/original").render()
        == f"tenant/{U}/documents/d1/original"
    )


def test_rate_limit_key_is_tenant_qualified_and_policy_validated() -> None:
    assert RateLimitKey(TenantId(U), "api:u1").render() == f"rl:{U}:api:u1"
    with pytest.raises(ValueError):
        RateLimitPolicy(capacity=0, refill_per_second=1)
