"""The egress use case with the REAL `EpochCheckedProofVerifier` on real Postgres (ADR-006, ADR-026).

Composition goes through `bootstrap` (the only place that may join modules). The provider is the
deterministic fake: the point is that no proof failure ever reaches it.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest

from lda.bootstrap.access_control import (
    ACCESS_CONTROL_MIGRATIONS,
    AccessControlContainer,
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.ai_gateway import build_ai_gateway, load_ai_gateway_settings
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
from lda.modules.ai_gateway.api import (
    GenerateText,
    GenerateTextCommand,
    GenerationParameters,
)
from lda.modules.ai_gateway.testing import FakeLlmProvider, build_segment
from lda.modules.identity.api import PrincipalKind, PrincipalReference, RegisterPrincipalCommand
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.testing.doubles import FakeClock, InMemoryAuditRecorder
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.errors import AuthorizationError
from lda.shared.kernel.ids import RequestId, TenantId
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofResource
from platform_support import Database, provisioned_database

ACTION = ActionId("documents.read_document")
KEY = base64.urlsafe_b64encode(b"ai-gateway-test-signing-key-32-bytes").decode().rstrip("=")
D1, D2 = ResourceRef("document", "d1"), ResourceRef("document", "d2")
T1, T2 = TenantId(UUID(int=1)), TenantId(UUID(int=2))
SECRET = "PRIVILEGED-TEXT-5519"  # noqa: S105 - marker text, not a credential


@pytest.fixture(scope="module")
def database() -> object:
    with provisioned_database(
        "lda_test_ai_gw", "lda_app_ai_gw", [IDENTITY_MIGRATIONS, ACCESS_CONTROL_MIGRATIONS]
    ) as db:
        yield db


@dataclass
class World:
    platform: PlatformContainer
    identity: IdentityContainer
    ac: AccessControlContainer
    gateway: GenerateText
    provider: FakeLlmProvider
    clock: FakeClock

    def principal(self, tenant: TenantId, name: str) -> TenantContext:
        who = self.identity.register_principal.execute(
            SystemContext(tenant, "ai-gateway-test"),
            RegisterPrincipalCommand(PrincipalKind.USER, name),
        )
        ctx = TenantContext(tenant, who.principal_id, RequestId(UUID(int=21)))
        for resource in (D1, D2):
            self.ac.grant_access.execute(
                ctx, GrantAccessCommand(who.principal_id, resource, ACTION)
            )
        return ctx

    def proof(self, ctx: TenantContext, *resources: ResourceRef) -> str:
        decision = self.ac.authorize_action.execute(
            ctx, AuthorizationRequest(ACTION, frozenset(resources or (D1, D2)))
        )
        assert decision.proof is not None
        return decision.proof.token


def command(token: str, *resources: ResourceRef, action: str = ACTION.value) -> GenerateTextCommand:
    return GenerateTextCommand(
        proof_token=Sensitive(token),
        action=action,
        model="fake-model-1",
        instructions=Sensitive("Summarize."),
        segments=tuple(
            build_segment(r.kind, r.id, f"{SECRET}-{r.id}") for r in (resources or (D1, D2))
        ),
        parameters=GenerationParameters(128, 0.0),
    )


@pytest.fixture
def world(database: Database) -> object:
    environment = {
        "LDA_DATABASE__URL": database.app_url,
        "LDA_OBJECT_STORAGE__BUCKET": "bucket",
        "LDA_JOBS__QUEUES": '{"events": 1}',
        "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
        "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
    }
    config = ConfigurationLoader(environment)
    ac_settings = load_access_control_settings(config)
    gw_settings = load_ai_gateway_settings(config)
    base = build_platform(config, audit_factory=lambda tx: InMemoryAuditRecorder())
    try:
        clock = FakeClock()
        platform = PlatformContainer(**{**base.__dict__, "clock": clock})
        identity = build_identity(platform)
        registry = ResourceRegistry()
        registry.register(ResourceKindDefinition("document", frozenset({ACTION})))
        ac = build_access_control(platform, identity, ac_settings, registry)
        provider = FakeLlmProvider()
        gateway = build_ai_gateway(
            platform, ac.proof_verifier, provider, gw_settings, sleep=lambda delay: None
        ).generate_text
        yield World(platform, identity, ac, gateway, provider, clock)
    finally:
        base.dispose()


def test_a_fresh_proof_reaches_the_provider_once_with_content_free_evidence(world: World) -> None:
    ctx = world.principal(T1, "Alice")
    result = world.gateway.execute(ctx, command(world.proof(ctx)))
    assert world.provider.call_count == 1
    assert result.evidence.resources == (
        ProofResource("document", "d1"),
        ProofResource("document", "d2"),
    )
    assert SECRET not in repr(result) and SECRET not in repr(result.evidence)


def test_a_stale_epoch_is_rejected_and_nothing_is_sent(world: World) -> None:
    ctx = world.principal(T1, "Bob")
    token = world.proof(ctx)
    world.ac.revoke_access.execute(ctx, RevokeAccessCommand(ctx.principal_id, D2, ACTION))
    with pytest.raises(AuthorizationError) as raised:
        world.gateway.execute(ctx, command(token))
    assert raised.value.code == "proof_stale_epoch" and world.provider.call_count == 0


def test_a_disabled_principal_cannot_egress_with_a_fresh_proof(world: World) -> None:
    ctx = world.principal(T1, "Carol")
    token = world.proof(ctx)
    world.identity.disable_principal.execute(ctx, PrincipalReference(ctx.principal_id))
    with pytest.raises(AuthorizationError) as raised:
        world.gateway.execute(ctx, command(token))
    assert raised.value.code == "proof_principal_inactive" and world.provider.call_count == 0


def test_an_expired_proof_is_rejected(world: World) -> None:
    ctx = world.principal(T1, "Dana")
    token = world.proof(ctx)
    world.clock.advance(timedelta(seconds=60))
    with pytest.raises(AuthorizationError) as raised:
        world.gateway.execute(ctx, command(token))
    assert raised.value.code == "proof_expired" and world.provider.call_count == 0


def test_a_proof_from_another_tenant_is_rejected(world: World) -> None:
    owner, other = world.principal(T1, "Erin"), world.principal(T2, "Erin")
    token = world.proof(owner)
    with pytest.raises(AuthorizationError) as raised:
        world.gateway.execute(
            other, command(token)
        )  # tenant 2's principal replays tenant 1's proof
    assert raised.value.code == "proof_mismatch" and world.provider.call_count == 0


def test_a_tampered_resource_set_or_action_is_rejected(world: World) -> None:
    ctx = world.principal(T1, "Frank")
    token = world.proof(ctx, D1)
    with pytest.raises(AuthorizationError) as extra:
        world.gateway.execute(ctx, command(token, D1, D2))  # proof covers D1 only
    with pytest.raises(AuthorizationError) as action:
        world.gateway.execute(ctx, command(token, D1, action="documents.delete_document"))
    assert extra.value.code == action.value.code == "proof_mismatch"
    assert world.provider.call_count == 0


def test_a_forged_or_malformed_token_is_rejected(world: World) -> None:
    ctx = world.principal(T1, "Grace")
    for token in ("not-a-proof", world.proof(ctx)[:-4] + "AAAA"):
        with pytest.raises(AuthorizationError):
            world.gateway.execute(ctx, command(token))
    assert world.provider.call_count == 0


def test_an_empty_scope_has_no_proof_and_is_refused(world: World) -> None:
    ctx = world.principal(T1, "Heidi")
    with pytest.raises(AuthorizationError) as raised:
        world.gateway.execute(
            ctx,
            GenerateTextCommand(
                Sensitive(world.proof(ctx)), ACTION.value, "fake-model-1", Sensitive("hello"), ()
            ),
        )
    assert raised.value.code == "egress_scope_empty" and world.provider.call_count == 0


def test_a_revocation_during_the_backoff_stops_the_retry(world: World) -> None:
    from lda.modules.ai_gateway.api import FailureKind

    ctx = world.principal(T1, "Ivan")
    token = world.proof(ctx)
    world.provider.fail_next(FailureKind.UNREACHABLE)

    def revoke_while_waiting(_: timedelta) -> None:
        world.ac.revoke_access.execute(ctx, RevokeAccessCommand(ctx.principal_id, D1, ACTION))

    gateway = GenerateText(
        world.ac.proof_verifier,
        world.provider,
        world.platform.rate_limiter,
        world.gateway._policy,  # noqa: SLF001
        revoke_while_waiting,
    )
    with pytest.raises(AuthorizationError) as raised:
        gateway.execute(ctx, command(token))
    assert raised.value.code == "proof_stale_epoch" and world.provider.call_count == 1
