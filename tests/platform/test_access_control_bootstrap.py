"""Access Control wired through the real composition root against real Postgres.

Identity is the real SQL adapter here (bootstrap may join modules; module tests may not). The
"gateway" below is a stand-in that, like ai_gateway at step 9, knows only the shared port.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from uuid import UUID

import pytest

from lda.bootstrap.access_control import (
    ACCESS_CONTROL_MIGRATIONS,
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.identity import IDENTITY_MIGRATIONS, IdentityContainer, build_identity
from lda.modules.access_control.api import (
    ActionId,
    AuthorizationRequest,
    GrantAccessCommand,
    ResourceKindDefinition,
    ResourceRef,
    ResourceRegistry,
    RevokeAccessCommand,
)
from lda.modules.identity.api import PrincipalKind, PrincipalReference, RegisterPrincipalCommand
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.testing.doubles import InMemoryAuditRecorder
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.errors import AuthorizationError, ConfigurationError
from lda.shared.kernel.ids import RequestId, TenantId
from lda.shared.ports.authorization import ProofExpectation, ProofResource, ProofVerifier
from platform_support import Database, provisioned_database

READ = ActionId("documents.read_document")
KEY = base64.urlsafe_b64encode(b"bootstrap-test-signing-key-32-bytes!!").decode().rstrip("=")
D1, D2 = ResourceRef("document", "d1"), ResourceRef("document", "d2")
T1, T2 = TenantId(UUID(int=1)), TenantId(UUID(int=2))


def environment(db: Database, **extra: str) -> dict[str, str]:
    return {
        "LDA_DATABASE__URL": db.app_url,
        "LDA_OBJECT_STORAGE__BUCKET": "bucket",
        "LDA_JOBS__QUEUES": '{"events": 1}',
        "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
        "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
        **extra,
    }


class Gateway:
    """What ai_gateway will look like to Access Control: a ProofVerifier and nothing else."""

    def __init__(self, verifier: ProofVerifier) -> None:
        self._verifier = verifier
        self.sent: list[str] = []

    def send(self, token: str, expected: ProofExpectation, text: str) -> None:
        self._verifier.verify(token, expected)  # raises -> the line below never runs
        self.sent.append(text)


def registry_with_documents() -> ResourceRegistry:
    registry = ResourceRegistry()  # the owning module registers through the AC API
    registry.register(ResourceKindDefinition("document", frozenset({READ})))
    return registry


@pytest.fixture(scope="module")
def database() -> object:
    with provisioned_database(
        "lda_test_ac_boot", "lda_app_ac_boot", [IDENTITY_MIGRATIONS, ACCESS_CONTROL_MIGRATIONS]
    ) as db:
        yield db


def test_end_to_end_authorization_revocation_and_egress(database: Database) -> None:
    config = ConfigurationLoader(environment(database))
    settings = load_access_control_settings(config)
    platform = build_platform(config, audit_factory=lambda tx: InMemoryAuditRecorder())
    try:
        identity = build_identity(platform)
        ac = build_access_control(platform, identity, settings, registry_with_documents())
        gateway = Gateway(ac.proof_verifier)

        admin = SystemContext(T1, "bootstrap-test")
        alice = identity.register_principal.execute(
            admin, RegisterPrincipalCommand(PrincipalKind.USER, "Alice")
        )
        ctx = TenantContext(T1, alice.principal_id, RequestId(UUID(int=9)))
        request = AuthorizationRequest(READ, frozenset({D1, D2}))

        assert not ac.authorize_action.execute(ctx, request).allowed  # default deny
        for resource in (D1, D2):
            ac.grant_access.execute(ctx, GrantAccessCommand(alice.principal_id, resource, READ))
        decision = ac.authorize_action.execute(ctx, request)
        assert decision.proof is not None and decision.scope is not None
        expected = ProofExpectation(
            tenant_id=T1,
            principal_id=alice.principal_id,
            action=READ.value,
            resources=frozenset(ProofResource(r.kind, r.id) for r in decision.scope.resources),
        )
        gateway.send(decision.proof.token, expected, "authorized text")
        assert gateway.sent == ["authorized text"]

        # Revocation commits -> the still-fresh proof is dead at the egress point.
        ac.revoke_access.execute(ctx, RevokeAccessCommand(alice.principal_id, D2, READ))
        with pytest.raises(AuthorizationError) as stale:
            gateway.send(decision.proof.token, expected, "must never be sent")
        assert stale.value.code == "proof_stale_epoch"
        assert gateway.sent == ["authorized text"]

        # A principal disabled now gets no new proof.
        identity.disable_principal.execute(ctx, PrincipalReference(alice.principal_id))
        assert not ac.authorize_action.execute(ctx, request).allowed
    finally:
        platform.dispose()


def test_a_proof_cannot_be_replayed_in_another_tenant(database: Database) -> None:
    config = ConfigurationLoader(environment(database))
    settings = load_access_control_settings(config)
    platform = build_platform(config, audit_factory=lambda tx: InMemoryAuditRecorder())
    try:
        identity = build_identity(platform)
        ac = build_access_control(platform, identity, settings, registry_with_documents())
        ctxs = {}
        for tenant in (T1, T2):
            who = identity.register_principal.execute(
                SystemContext(tenant, "bootstrap-test"),
                RegisterPrincipalCommand(PrincipalKind.USER, "Same Name"),
            )
            ctxs[tenant] = TenantContext(tenant, who.principal_id, RequestId(UUID(int=5)))
            ac.grant_access.execute(ctxs[tenant], GrantAccessCommand(who.principal_id, D1, READ))
        decision = ac.authorize_action.execute(
            ctxs[T1], AuthorizationRequest(READ, frozenset({D1}))
        )
        assert decision.proof is not None
        as_tenant_two = ProofExpectation(
            tenant_id=T2,
            principal_id=ctxs[T2].principal_id,
            action=READ.value,
            resources=frozenset({ProofResource("document", "d1")}),
        )
        with pytest.raises(AuthorizationError) as caught:
            ac.proof_verifier.verify(decision.proof.token, as_tenant_two)
        assert caught.value.code == "proof_mismatch"
    finally:
        platform.dispose()


def test_proofs_expire_after_sixty_seconds_in_the_real_wiring(database: Database) -> None:
    from lda.platform.testing.doubles import FakeClock

    config = ConfigurationLoader(environment(database))
    settings = load_access_control_settings(config)
    platform = build_platform(config, audit_factory=lambda tx: InMemoryAuditRecorder())
    try:
        clock = FakeClock()
        container = PlatformContainer(**{**platform.__dict__, "clock": clock})
        identity: IdentityContainer = build_identity(container)
        ac = build_access_control(container, identity, settings, registry_with_documents())
        who = identity.register_principal.execute(
            SystemContext(T1, "bootstrap-test"),
            RegisterPrincipalCommand(PrincipalKind.SERVICE, "svc"),
        )
        ctx = TenantContext(T1, who.principal_id, RequestId(UUID(int=7)))
        ac.grant_access.execute(ctx, GrantAccessCommand(who.principal_id, D1, READ))
        decision = ac.resolve_access_scope.execute(ctx, AuthorizationRequest(READ, frozenset({D1})))
        assert decision.proof is not None
        expected = ProofExpectation(
            tenant_id=T1,
            principal_id=who.principal_id,
            action=READ.value,
            resources=frozenset({ProofResource("document", "d1")}),
        )
        ac.proof_verifier.verify(decision.proof.token, expected)
        clock.advance(timedelta(seconds=60))
        with pytest.raises(AuthorizationError) as caught:
            ac.proof_verifier.verify(decision.proof.token, expected)
        assert caught.value.code == "proof_expired"
    finally:
        platform.dispose()


def test_the_registry_is_sealed_by_composition(database: Database) -> None:
    config = ConfigurationLoader(environment(database))
    settings = load_access_control_settings(config)
    platform = build_platform(config, audit_factory=lambda tx: InMemoryAuditRecorder())
    try:
        registry = registry_with_documents()
        build_access_control(platform, build_identity(platform), settings, registry)
        assert registry.sealed
        from lda.modules.access_control.domain.errors import InvalidRegistration

        with pytest.raises(InvalidRegistration):
            registry.register(ResourceKindDefinition("folder", frozenset({READ})))
    finally:
        platform.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"LDA_ACCESS_CONTROL__PROOF_KEY": "c2hvcnQ"},  # 5 bytes
        {"LDA_ACCESS_CONTROL__PROOF_KEY_ID": "Not Valid"},
    ],
)
def test_a_missing_or_weak_key_fails_before_any_connection_is_made(
    database: Database, overrides: dict[str, str]
) -> None:
    config = ConfigurationLoader(environment(database, **overrides))
    with pytest.raises(ConfigurationError):
        load_access_control_settings(config)


def test_an_absent_slice_fails_closed() -> None:
    with pytest.raises(ConfigurationError):
        load_access_control_settings(ConfigurationLoader({}))


def test_disabling_a_principal_kills_its_fresh_proof_without_touching_the_epoch(
    database: Database,
) -> None:
    """Real SQL Identity: a proof minted while active is rejected right after the disable."""
    config = ConfigurationLoader(environment(database))
    settings = load_access_control_settings(config)
    platform = build_platform(config, audit_factory=lambda tx: InMemoryAuditRecorder())
    try:
        identity = build_identity(platform)
        ac = build_access_control(platform, identity, settings, registry_with_documents())
        gateway = Gateway(ac.proof_verifier)
        who = identity.register_principal.execute(
            SystemContext(T1, "bootstrap-test"),
            RegisterPrincipalCommand(PrincipalKind.USER, "Dana"),
        )
        ctx = TenantContext(T1, who.principal_id, RequestId(UUID(int=11)))
        ac.grant_access.execute(ctx, GrantAccessCommand(who.principal_id, D1, READ))
        decision = ac.authorize_action.execute(ctx, AuthorizationRequest(READ, frozenset({D1})))
        assert decision.proof is not None and decision.scope is not None
        expected = ProofExpectation(
            tenant_id=T1,
            principal_id=who.principal_id,
            action=READ.value,
            resources=frozenset({ProofResource("document", "d1")}),
        )
        gateway.send(decision.proof.token, expected, "sent while active")
        identity.disable_principal.execute(ctx, PrincipalReference(who.principal_id))
        with pytest.raises(AuthorizationError) as caught:
            gateway.send(decision.proof.token, expected, "must never be sent")
        assert caught.value.code == "proof_principal_inactive"
        assert gateway.sent == ["sent while active"]
        # Only a grant change moves the epoch; the disable did not (the next grant gets +1).
        change = ac.grant_access.execute(ctx, GrantAccessCommand(who.principal_id, D2, READ))
        assert change.policy_epoch == decision.scope.policy_epoch.value + 1
    finally:
        platform.dispose()
