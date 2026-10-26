"""Public surface of access_control (WHAT may a principal do?). The only importable package."""

from lda.modules.access_control.application.dto.access import (
    AuthorizationRequest,
    GrantAccessCommand,
    PolicyChangeView,
    RevokeAccessCommand,
)
from lda.modules.access_control.application.policy_reader import PolicyReader
from lda.modules.access_control.application.proof_verification import EpochCheckedProofVerifier
from lda.modules.access_control.application.queries.authorize_action import AuthorizeAction
from lda.modules.access_control.application.queries.resolve_access_scope import (
    ResolveAccessScope,
)
from lda.modules.access_control.application.use_cases.grant_access import GrantAccess
from lda.modules.access_control.application.use_cases.revoke_access import RevokeAccess
from lda.modules.access_control.domain.decision import AuthorizationDecision, DenyReason, Effect
from lda.modules.access_control.domain.epoch import PolicyEpoch
from lda.modules.access_control.domain.proof import (
    PROOF_LIFETIME,
    AuthorizationProof,
    SigningKey,
    SigningKeyring,
)
from lda.modules.access_control.domain.registry import ResourceKindDefinition, ResourceRegistry
from lda.modules.access_control.domain.resource import ActionId, ResourceRef
from lda.modules.access_control.domain.scope import MAX_SCOPE_RESOURCES, AccessScope

__all__ = [
    "MAX_SCOPE_RESOURCES",
    "PROOF_LIFETIME",
    "AccessScope",
    "ActionId",
    "AuthorizationDecision",
    "AuthorizationProof",
    "AuthorizationRequest",
    "AuthorizeAction",
    "DenyReason",
    "Effect",
    "EpochCheckedProofVerifier",
    "GrantAccess",
    "GrantAccessCommand",
    "PolicyChangeView",
    "PolicyEpoch",
    "PolicyReader",
    "ResolveAccessScope",
    "ResourceKindDefinition",
    "ResourceRef",
    "ResourceRegistry",
    "RevokeAccess",
    "RevokeAccessCommand",
    "SigningKey",
    "SigningKeyring",
]
