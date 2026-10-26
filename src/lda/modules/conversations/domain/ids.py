"""Typed identifiers of the conversations context."""

from __future__ import annotations

from dataclasses import dataclass

from lda.shared.kernel.ids import EntityId


@dataclass(frozen=True, slots=True)
class ConversationId(EntityId):
    """One conversation (a tenant-scoped, owner-scoped message log)."""


@dataclass(frozen=True, slots=True)
class ClientMessageId(EntityId):
    """Caller-chosen idempotency identifier of one user message within one conversation."""
