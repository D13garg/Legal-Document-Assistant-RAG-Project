from __future__ import annotations

from lda.modules.access_control.domain.epoch import PolicyEpoch
from lda.modules.access_control.domain.proof import SigningKey, SigningKeyring
from lda.modules.access_control.domain.registry import ResourceKindDefinition, ResourceRegistry
from lda.modules.access_control.domain.resource import ActionId, ResourceRef
from lda.modules.access_control.domain.scope import AccessScope
from lda.shared.kernel.ids import PrincipalId, TenantId

TEST_KEY_ID = "test-key-1"
TEST_SECRET = b"unit-test-signing-secret-0123456789"  # 35 bytes; a throwaway test value


def a_signing_keyring(*, key_id: str = TEST_KEY_ID, secret: bytes = TEST_SECRET) -> SigningKeyring:
    return SigningKeyring(SigningKey(key_id, secret))


def a_resource_registry(*definitions: ResourceKindDefinition) -> ResourceRegistry:
    """A SEALED registry. Defaults to a `document` kind with read and delete actions."""
    registry = ResourceRegistry()
    for definition in definitions or (a_document_kind(),):
        registry.register(definition)
    registry.seal()
    return registry


def a_document_kind() -> ResourceKindDefinition:
    return ResourceKindDefinition(
        "document",
        frozenset({ActionId("documents.read_document"), ActionId("documents.delete_document")}),
    )


def an_access_scope(
    *,
    tenant_id: TenantId,
    principal_id: PrincipalId,
    resources: frozenset[ResourceRef],
    policy_epoch: int = 0,
) -> AccessScope:
    """The only sanctioned way for other modules' tests to obtain an AccessScope (AG090)."""
    return AccessScope(
        tenant_id=tenant_id,
        principal_id=principal_id,
        policy_epoch=PolicyEpoch(policy_epoch),
        resources=resources,
    )
