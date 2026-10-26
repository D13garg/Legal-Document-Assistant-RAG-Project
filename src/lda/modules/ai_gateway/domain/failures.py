"""Provider failure taxonomy (ADR-006). Failures carry a kind and a stable code, never detail.

The kind decides retry safety:
- UNREACHABLE: failed before any byte of the request reached the provider (connection refused,
  DNS failure, TLS handshake failure). The only kind that may be retried.
- TIMEOUT: the provider may already have received or processed the request. Not retried.
- REJECTED: the provider refused the request (authentication, content policy, bad parameters).
  Permanent. Not retried.
- AMBIGUOUS: anything else after the request may have been sent (5xx, dropped connection,
  malformed reply). Not retried: the gateway never claims exactly-once execution.
"""

from __future__ import annotations

from enum import Enum

from lda.modules.ai_gateway.domain import codes
from lda.shared.kernel.errors import ApplicationError, ExternalProviderError


class FailureKind(Enum):
    UNREACHABLE = codes.PROVIDER_UNREACHABLE
    TIMEOUT = codes.PROVIDER_TIMEOUT
    REJECTED = codes.PROVIDER_REJECTED
    AMBIGUOUS = codes.PROVIDER_AMBIGUOUS

    @property
    def retryable(self) -> bool:
        return self is FailureKind.UNREACHABLE


class ProviderFailure(ExternalProviderError):
    """Raised by provider adapters. Adapters must translate every SDK error into one of these and
    must not copy SDK messages (they can echo request content) into it."""

    def __init__(self, kind: FailureKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


class RateLimited(ApplicationError):
    """The tenant's egress budget is exhausted. `retry_after_seconds` is advice only."""

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(codes.RATE_LIMITED)
        self.retry_after_seconds = retry_after_seconds
