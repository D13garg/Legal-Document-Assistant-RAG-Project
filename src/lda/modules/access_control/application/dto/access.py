from __future__ import annotations

from dataclasses import dataclass

from lda.modules.access_control.domain.resource import ActionId, ResourceRef
from lda.modules.access_control.domain.scope import MAX_SCOPE_RESOURCES
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import PrincipalId


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """What the caller wants to do. The principal and tenant come from the execution context."""

    action: ActionId
    resources: frozenset[ResourceRef]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.resources, frozenset)
            or not self.resources
            or len(self.resources) > MAX_SCOPE_RESOURCES
        ):
            raise ValidationError("authorization_resources_invalid")


@dataclass(frozen=True, slots=True)
class GrantAccessCommand:
    principal_id: PrincipalId
    resource: ResourceRef
    action: ActionId


@dataclass(frozen=True, slots=True)
class RevokeAccessCommand:
    principal_id: PrincipalId
    resource: ResourceRef
    action: ActionId


@dataclass(frozen=True, slots=True)
class PolicyChangeView:
    """`changed` is False for an idempotent repeat; the epoch then did not move."""

    changed: bool
    policy_epoch: int
