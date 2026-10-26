"""Audit wired through the real composition root against real Postgres (ADR-019).

Identity and Access Control run on their real SQL adapters with the real `SqlAuditRecorder`:
the audit row must commit and roll back with the action's own transaction, and an audit failure
must fail the action.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.bootstrap.access_control import (
    ACCESS_CONTROL_MIGRATIONS,
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.audit import AUDIT_MIGRATIONS, build_audit
from lda.bootstrap.container import build_platform
from lda.bootstrap.identity import IDENTITY_MIGRATIONS, build_identity
from lda.modules.access_control.api import (
    ActionId,
    GrantAccessCommand,
    ResourceKindDefinition,
    ResourceRef,
    ResourceRegistry,
    RevokeAccessCommand,
)
from lda.modules.identity.api import PrincipalKind, PrincipalReference, RegisterPrincipalCommand
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import RequestId, TenantId
from lda.shared.ports.audit import AuditFact
from platform_support import Database, provisioned_database

READ = ActionId("documents.read_document")
D1 = ResourceRef("document", "d1")
KEY = base64.urlsafe_b64encode(b"audit-bootstrap-test-key-32-bytes!!").decode().rstrip("=")
T1, T2 = TenantId(UUID(int=1)), TenantId(UUID(int=2))


def environment(db: Database) -> dict[str, str]:
    return {
        "LDA_DATABASE__URL": db.app_url,
        "LDA_OBJECT_STORAGE__BUCKET": "bucket",
        "LDA_JOBS__QUEUES": '{"events": 1}',
        "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
        "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
    }


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_audit_boot",
        "lda_app_audit_boot",
        [IDENTITY_MIGRATIONS, ACCESS_CONTROL_MIGRATIONS, AUDIT_MIGRATIONS],
    ) as db:
        yield db


@pytest.fixture
def oracle(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM audit.records"))
    yield engine
    engine.dispose()


def rows(oracle: Engine) -> list[tuple[UUID, str, str, str | None, dict[str, str]]]:
    with oracle.connect() as conn:
        found = conn.execute(
            text(
                "SELECT tenant_id, action, resource_type, resource_id, attributes "
                "FROM audit.records ORDER BY record_id"
            )
        ).all()
    return [tuple(r) for r in found]


def registry() -> ResourceRegistry:
    reg = ResourceRegistry()
    reg.register(ResourceKindDefinition("document", frozenset({READ})))
    return reg


def test_every_audited_use_case_writes_its_row_in_its_own_transaction(
    database: Database, oracle: Engine
) -> None:
    config = ConfigurationLoader(environment(database))
    settings = load_access_control_settings(config)
    audit = build_audit()
    platform = build_platform(config, audit_factory=audit.recorder_factory)
    try:
        identity = build_identity(platform)
        ac = build_access_control(platform, identity, settings, registry())
        admin = SystemContext(T1, "audit-bootstrap-test")
        alice = identity.register_principal.execute(
            admin, RegisterPrincipalCommand(PrincipalKind.USER, "Alice Secret-Name")
        )
        ctx = TenantContext(T1, alice.principal_id, RequestId(UUID(int=9)))
        ac.grant_access.execute(ctx, GrantAccessCommand(alice.principal_id, D1, READ))
        ac.grant_access.execute(ctx, GrantAccessCommand(alice.principal_id, D1, READ))  # no-op
        ac.revoke_access.execute(ctx, RevokeAccessCommand(alice.principal_id, D1, READ))
        ac.revoke_access.execute(ctx, RevokeAccessCommand(alice.principal_id, D1, READ))  # no-op
        identity.disable_principal.execute(ctx, PrincipalReference(alice.principal_id))
        identity.disable_principal.execute(ctx, PrincipalReference(alice.principal_id))  # no-op
        identity.enable_principal.execute(ctx, PrincipalReference(alice.principal_id))
        # Reads and decisions leave no trace in Step 4 (ADR-019 D3).
        identity.get_principal.execute(ctx, PrincipalReference(alice.principal_id))

        found = rows(oracle)
        assert [r[1] for r in found] == [
            "identity.principal_registered",
            "access_control.access_granted",
            "access_control.access_revoked",
            "identity.principal_disabled",
            "identity.principal_enabled",
        ]
        assert {r[0] for r in found} == {T1.value}
        assert found[0][4] == {"purpose": "audit-bootstrap-test", "principal_kind": "user"}
        assert found[1][2:4] == ("document", "d1")
        assert "Secret-Name" not in repr(found)
    finally:
        platform.dispose()


def test_an_audit_failure_fails_the_action_and_nothing_persists(
    database: Database, oracle: Engine
) -> None:
    config = ConfigurationLoader(environment(database))

    class Failing:
        def record(self, fact: AuditFact) -> None:
            raise InfrastructureError("audit_persistence_failed")

    platform = build_platform(config, audit_factory=lambda tx: Failing())
    try:
        identity = build_identity(platform)
        with pytest.raises(InfrastructureError) as caught:
            identity.register_principal.execute(
                SystemContext(T2, "audit-failure"),
                RegisterPrincipalCommand(PrincipalKind.USER, "Ghost"),
            )
        assert caught.value.code == "audit_persistence_failed"
        with oracle.connect() as conn:
            ghosts = conn.execute(
                text("SELECT count(*) FROM identity.principals WHERE tenant_id = :t"),
                {"t": str(T2)},
            ).scalar_one()
        assert ghosts == 0  # the principal was rolled back with the failed audit write
        assert rows(oracle) == []
    finally:
        platform.dispose()


def test_two_tenants_are_recorded_separately_and_neither_can_read_them(
    database: Database, oracle: Engine
) -> None:
    config = ConfigurationLoader(environment(database))
    platform = build_platform(config, audit_factory=build_audit().recorder_factory)
    try:
        identity = build_identity(platform)
        for tenant in (T1, T2):
            identity.register_principal.execute(
                SystemContext(tenant, "audit-isolation"),
                RegisterPrincipalCommand(PrincipalKind.USER, "Same Name"),
            )
        assert {r[0] for r in rows(oracle)} == {T1.value, T2.value}
        with platform.units_of_work.begin(SystemContext(T1, "reader")) as uow:  # runtime role
            assert uow.tenant_id == T1
        from sqlalchemy.exc import DBAPIError

        runtime = create_engine(database.app_url)
        try:
            with pytest.raises(DBAPIError), runtime.begin() as conn:
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(T1)})
                conn.execute(text("SELECT count(*) FROM audit.records"))
        finally:
            runtime.dispose()
    finally:
        platform.dispose()


def test_the_platform_still_refuses_to_start_without_an_audit_factory() -> None:
    import inspect

    from lda.bootstrap.container import build_platform as build

    parameter = inspect.signature(build).parameters["audit_factory"]
    assert parameter.default is inspect.Parameter.empty  # audit is never silently absent
