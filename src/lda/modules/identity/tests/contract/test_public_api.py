"""A consumer can use Identity through api/ + testing/ alone, and nothing internal leaks."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import lda.modules.identity.api as api
from lda.modules.identity.api import (
    GetPrincipal,
    PrincipalKind,
    PrincipalReference,
    PrincipalView,
    RegisterPrincipal,
    RegisterPrincipalCommand,
)
from lda.modules.identity.testing import InMemoryIdentityUnitOfWorkFactory
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.context import ExecutionContext

EXPECTED_SURFACE = {
    "DisablePrincipal",
    "EnablePrincipal",
    "GetPrincipal",
    "PrincipalKind",
    "PrincipalReference",
    "PrincipalStatus",
    "PrincipalView",
    "RegisterPrincipal",
    "RegisterPrincipalCommand",
}


def test_surface_is_exactly_the_approved_contract() -> None:
    assert set(api.__all__) == EXPECTED_SURFACE
    assert all(hasattr(api, name) for name in api.__all__)


def test_a_consumer_can_register_and_read_using_only_public_names() -> None:
    factory = InMemoryIdentityUnitOfWorkFactory()
    register = RegisterPrincipal(factory, FakeClock(), SequentialIdGenerator())
    ctx: ExecutionContext = _ctx()
    view = register.execute(ctx, RegisterPrincipalCommand(PrincipalKind.USER, "Consumer"))
    assert isinstance(view, PrincipalView)
    assert GetPrincipal(factory).execute(ctx, PrincipalReference(view.principal_id)) == view


def test_views_expose_identity_facts_only() -> None:
    names = {f.name for f in dataclasses.fields(PrincipalView)}
    assert names == {
        "principal_id",
        "tenant_id",
        "kind",
        "display_name",
        "status",
        "created_at",
        "status_changed_at",
    }
    forbidden = ("password", "secret", "token", "credential", "permission", "role", "version")
    assert not [n for n in names if any(word in n for word in forbidden)]


def test_api_package_imports_nothing_from_infrastructure_or_third_parties() -> None:
    source = Path(api.__file__ or "").read_text(encoding="utf-8")
    modules = [n.module or "" for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom)]
    assert modules and all(m.startswith("lda.modules.identity.") for m in modules)
    assert not [m for m in modules if ".infrastructure" in m or ".testing" in m]


def _ctx() -> ExecutionContext:
    from uuid import UUID

    from lda.shared.kernel.context import SystemContext
    from lda.shared.kernel.ids import TenantId

    return SystemContext(TenantId(UUID(int=1)), "consumer-contract-test")
