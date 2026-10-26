"""Stable error codes of the egress boundary. Codes carry no content (ADR-006)."""

from __future__ import annotations

INVALID_REQUEST = "egress_invalid_request"
LIMIT_EXCEEDED = "egress_limit_exceeded"
SCOPE_EMPTY = "egress_scope_empty"
RATE_LIMITED = "egress_rate_limited"

PROVIDER_UNREACHABLE = "provider_unreachable"
PROVIDER_TIMEOUT = "provider_timeout"
PROVIDER_REJECTED = "provider_rejected"
PROVIDER_AMBIGUOUS = "provider_failed_ambiguous"
