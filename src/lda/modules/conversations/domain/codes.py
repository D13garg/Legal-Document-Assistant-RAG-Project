"""Stable, content-free error codes. No code ever carries stored or submitted content."""

from __future__ import annotations

NOT_FOUND = "conversation_not_found"
"""Missing, other-tenant and not-owned conversations all look like this (ADR-039 C4)."""
REQUIRES_PRINCIPAL = "conversation_requires_principal"
MESSAGE_INVALID = "conversation_message_invalid"
MESSAGE_CONFLICT = "conversation_message_conflict"
LIMIT_REACHED = "conversation_limit_reached"
RECORD_UNAVAILABLE = "conversation_record_unavailable"
RECORD_LINKED = "conversation_record_linked"
STATE_CHANGED = "conversation_state_changed"
EXISTS = "conversation_exists"
INVALID = "conversation_invalid"
LIMITS_INVALID = "conversation_limits_invalid"
PERSISTENCE_FAILED = "conversations_persistence_failed"
CORRUPT = "conversations_row_corrupt"
TENANT_MISMATCH = "conversations_tenant_mismatch"
