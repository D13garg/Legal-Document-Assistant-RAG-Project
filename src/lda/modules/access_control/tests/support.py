"""Shared constants and the wiring rig for Access Control's tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from lda.modules.access_control.api import (
    AuthorizeAction,
    EpochCheckedProofVerifier,
    GrantAccess,
    PolicyReader,
    ResolveAccessScope,
    ResourceRegistry,
    RevokeAccess,
    SigningKeyring,
)
from lda.modules.access_control.application.ports import AccessControlUnitOfWorkFactory
from lda.modules.access_control.testing import a_resource_registry, a_signing_keyring
from lda.modules.identity.api import DisablePrincipal, GetPrincipal, PrincipalReference
from lda.modules.identity.testing import InMemoryIdentityUnitOfWorkFactory, a_principal
from lda.platform.testing.doubles import FakeClock
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId

TENANT_A = TenantId(UUID(int=0xA1))
TENANT_B = TenantId(UUID(int=0xB2))
ALICE = PrincipalId(UUID(int=0xA11CE))
BOB = PrincipalId(UUID(int=0xB0B))
CAROL = PrincipalId(UUID(int=0xCA201))  # the active principal of tenant B
START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def ctx_for(tenant: TenantId, principal: PrincipalId, n: int = 1) -> TenantContext:
    return TenantContext(tenant, principal, RequestId(UUID(int=n)))


@dataclass
class Rig:
    """Access Control wired to an in-memory Identity and a given policy unit-of-work factory."""

    clock: FakeClock
    identity: InMemoryIdentityUnitOfWorkFactory
    policy: AccessControlUnitOfWorkFactory
    registry: ResourceRegistry
    keyring: SigningKeyring
    grant: GrantAccess
    revoke: RevokeAccess
    authorize: AuthorizeAction
    resolve: ResolveAccessScope
    verifier: EpochCheckedProofVerifier
    disable: DisablePrincipal


def build_rig(
    identity: InMemoryIdentityUnitOfWorkFactory,
    policy: AccessControlUnitOfWorkFactory,
    clock: FakeClock,
) -> Rig:
    registry, keyring = a_resource_registry(), a_signing_keyring()
    get_principal = GetPrincipal(identity)
    reader = PolicyReader(policy, get_principal, registry)
    for tenant, principal in ((TENANT_A, ALICE), (TENANT_A, BOB), (TENANT_B, CAROL)):
        with identity.begin(ctx_for(tenant, principal)) as uow:
            uow.principals.add(a_principal(tenant_id=tenant, id=principal, now=START))
            uow.commit()
    return Rig(
        clock=clock,
        identity=identity,
        policy=policy,
        registry=registry,
        keyring=keyring,
        grant=GrantAccess(policy, get_principal, registry, clock),
        revoke=RevokeAccess(policy, clock),
        authorize=AuthorizeAction(reader, clock, keyring),
        resolve=ResolveAccessScope(reader, clock, keyring),
        verifier=EpochCheckedProofVerifier(keyring, policy, get_principal, clock),
        disable=DisablePrincipal(identity, clock),
    )


def disable_principal(rig: Rig, tenant: TenantId, principal: PrincipalId) -> None:
    rig.disable.execute(ctx_for(tenant, principal), PrincipalReference(principal))
