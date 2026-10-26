"""The resource registry: which actions exist for which resource kinds (ADR-014 §3).

Code-defined and in-process. Owning modules register from their bootstrap wiring; composition
seals it. Lookups fail closed on an unsealed registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.access_control.domain.errors import InvalidRegistration
from lda.modules.access_control.domain.resource import ActionId, is_valid_kind


@dataclass(frozen=True, slots=True)
class ResourceKindDefinition:
    kind: str
    actions: frozenset[ActionId]

    def __post_init__(self) -> None:
        if not is_valid_kind(self.kind):
            raise InvalidRegistration("resource_kind_invalid")
        if not self.actions or not all(isinstance(a, ActionId) for a in self.actions):
            raise InvalidRegistration("resource_kind_actions_invalid")


class ResourceRegistry:
    def __init__(self) -> None:
        self._kinds: dict[str, frozenset[ActionId]] = {}
        self._sealed = False

    def register(self, definition: ResourceKindDefinition) -> None:
        if self._sealed:
            raise InvalidRegistration("resource_registry_sealed")
        if definition.kind in self._kinds:
            raise InvalidRegistration("resource_kind_registered")
        self._kinds[definition.kind] = definition.actions

    def seal(self) -> None:
        self._sealed = True

    @property
    def sealed(self) -> bool:
        return self._sealed

    def allows(self, kind: str, action: ActionId) -> bool:
        """True only if `action` is registered for `kind`. Unknown kinds are never allowed."""
        if not self._sealed:
            raise InvalidRegistration("resource_registry_not_sealed")
        return action in self._kinds.get(kind, frozenset())
