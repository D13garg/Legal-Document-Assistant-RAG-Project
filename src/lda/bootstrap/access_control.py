"""Access Control wiring: the only place that joins its use cases to the SQL adapters and keys.

Also the injection point for the proof verifier: it is returned typed as the shared
`ProofVerifier` port, which is all `ai_gateway` (step 9) will ever see (ADR-026 §6).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lda.bootstrap.container import PlatformContainer
from lda.bootstrap.identity import IdentityContainer
from lda.modules.access_control.api import (
    AuthorizeAction,
    EpochCheckedProofVerifier,
    GrantAccess,
    PolicyReader,
    ResolveAccessScope,
    ResourceRegistry,
    RevokeAccess,
    SigningKey,
    SigningKeyring,
)
from lda.modules.access_control.config import AccessControlSettings
from lda.modules.access_control.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.access_control.infrastructure.persistence.unit_of_work import (
    SqlAccessControlUnitOfWorkFactory,
    SqlResourceAccessRevoker,
)
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.database.transaction import TenantTransaction
from lda.shared.ports.access_revoker import ResourceAccessRevoker
from lda.shared.ports.authorization import ProofVerifier

ACCESS_CONTROL_MIGRATIONS = MIGRATIONS_VERSIONS
"""Pass as `extra_locations` to `platform.database.migrate.upgrade` (creates the schema)."""


@dataclass(frozen=True)
class AccessControlContainer:
    registry: ResourceRegistry
    grant_access: GrantAccess
    revoke_access: RevokeAccess
    authorize_action: AuthorizeAction
    resolve_access_scope: ResolveAccessScope
    proof_verifier: ProofVerifier
    revoker_factory: Callable[[TenantTransaction], ResourceAccessRevoker]
    """Hand to an owning module's unit of work so its deletes revoke access atomically (ADR-023)."""


def load_access_control_settings(config: ConfigurationLoader) -> AccessControlSettings:
    """Validate the signing slice. Call it BEFORE `build_platform` creates the connection pool, so a
    missing or weak key fails startup without leaking resources (ADR-031 fail-fast)."""
    return config.load("access_control", AccessControlSettings)


def build_access_control(
    platform: PlatformContainer,
    identity: IdentityContainer,
    settings: AccessControlSettings,
    registry: ResourceRegistry,
) -> AccessControlContainer:
    """`registry` must already hold every owning module's kinds; it is sealed here."""
    keyring = SigningKeyring(SigningKey(settings.proof_key_id, settings.signing_secret()))
    registry.seal()
    units_of_work = SqlAccessControlUnitOfWorkFactory(platform.units_of_work)
    policy = PolicyReader(units_of_work, identity.get_principal, registry)
    return AccessControlContainer(
        registry=registry,
        grant_access=GrantAccess(units_of_work, identity.get_principal, registry, platform.clock),
        revoke_access=RevokeAccess(units_of_work, platform.clock),
        authorize_action=AuthorizeAction(policy, platform.clock, keyring),
        resolve_access_scope=ResolveAccessScope(policy, platform.clock, keyring),
        proof_verifier=EpochCheckedProofVerifier(
            keyring, units_of_work, identity.get_principal, platform.clock
        ),
        revoker_factory=SqlResourceAccessRevoker,
    )
