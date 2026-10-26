from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from lda.modules.identity.domain.display_name import DisplayName
from lda.modules.identity.domain.errors import InvalidPrincipal, InvalidStatusTransition
from lda.modules.identity.domain.principal import Principal, PrincipalKind, PrincipalStatus
from lda.modules.identity.testing import a_principal
from lda.shared.kernel.ids import PrincipalId, TenantId

T0 = datetime(2026, 1, 1, tzinfo=UTC)
TENANT = TenantId(UUID(int=7))


def test_registration_starts_active_at_version_one() -> None:
    p = a_principal(tenant_id=TENANT, now=T0)
    assert (p.status, p.version, p.is_active) == (PrincipalStatus.ACTIVE, 1, True)
    assert p.created_at == p.status_changed_at == T0
    assert p.tenant_id == TENANT


def test_disable_then_enable_moves_status_and_version() -> None:
    p = a_principal(tenant_id=TENANT, now=T0)
    later = T0 + timedelta(minutes=5)
    off = p.disable(later)
    assert (off.status, off.version, off.status_changed_at) == (PrincipalStatus.DISABLED, 2, later)
    assert off.is_active is False
    on = off.enable(later + timedelta(minutes=1))
    assert (on.status, on.version) == (PrincipalStatus.ACTIVE, 3)


def test_lifecycle_moves_never_change_identity_fields() -> None:
    p = a_principal(tenant_id=TENANT, kind=PrincipalKind.SERVICE, now=T0)
    moved = p.disable(T0 + timedelta(hours=1)).enable(T0 + timedelta(hours=2))
    assert (moved.id, moved.tenant_id, moved.kind, moved.created_at) == (
        p.id,
        p.tenant_id,
        p.kind,
        p.created_at,
    )
    assert moved.display_name == p.display_name


def test_original_is_not_mutated() -> None:
    p = a_principal(tenant_id=TENANT, now=T0)
    p.disable(T0)
    assert p.status is PrincipalStatus.ACTIVE


def test_invalid_transitions_are_rejected() -> None:
    p = a_principal(tenant_id=TENANT, now=T0)
    with pytest.raises(InvalidStatusTransition):
        p.enable(T0)  # already active
    off = p.disable(T0)
    with pytest.raises(InvalidStatusTransition):
        off.disable(T0)  # already disabled


def test_principal_is_immutable() -> None:
    p = a_principal(tenant_id=TENANT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.status = PrincipalStatus.DISABLED  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.tenant_id = TenantId(UUID(int=9))  # type: ignore[misc]


def _build(**overrides: object) -> Principal:
    fields: dict[str, object] = {
        "id": PrincipalId(UUID(int=1)),
        "tenant_id": TENANT,
        "kind": PrincipalKind.USER,
        "display_name": DisplayName("n"),
        "status": PrincipalStatus.ACTIVE,
        "version": 1,
        "created_at": T0,
        "status_changed_at": T0,
    }
    fields.update(overrides)
    return Principal(**fields)  # type: ignore[arg-type]


def test_invariants_are_enforced_on_construction() -> None:
    _build()
    with pytest.raises(InvalidPrincipal, match="version"):
        _build(version=0)
    with pytest.raises(InvalidPrincipal, match="naive"):
        _build(created_at=datetime(2026, 1, 1), status_changed_at=datetime(2026, 1, 1))
    with pytest.raises(InvalidPrincipal, match="order"):
        _build(status_changed_at=T0 - timedelta(seconds=1))


def test_clock_going_backwards_cannot_produce_an_invalid_principal() -> None:
    p = a_principal(tenant_id=TENANT, now=T0)
    with pytest.raises(InvalidPrincipal, match="order"):
        p.disable(T0 - timedelta(seconds=1))


def test_repr_hides_display_name() -> None:
    p = a_principal(tenant_id=TENANT, display_name="Jane Q. Confidential")
    assert "Confidential" not in repr(p)
