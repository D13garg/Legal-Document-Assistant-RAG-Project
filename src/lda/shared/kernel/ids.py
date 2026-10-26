"""Typed identifiers. A tenant id can never be passed where a principal id is expected."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class EntityId:
    value: UUID

    @classmethod
    def parse(cls, raw: str) -> EntityId:
        return cls(UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class TenantId(EntityId):
    """An organization/tenant. Every tenant-sensitive record carries one."""


@dataclass(frozen=True, slots=True)
class PrincipalId(EntityId):
    """An authenticated actor (user or service)."""


@dataclass(frozen=True, slots=True)
class RequestId(EntityId):
    """Correlates one inbound request across logs, audit and jobs."""


@dataclass(frozen=True, slots=True)
class EventId(EntityId):
    """Identity of a domain event; the basis of consumer idempotency."""


@dataclass(frozen=True, slots=True)
class JobId(EntityId):
    """Identity of a queued job."""
