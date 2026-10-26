"""Error taxonomy. Presentation maps categories to responses; `detail` is never shown to clients."""

from __future__ import annotations


class LdaError(Exception):
    """Base for all expected failures. `code` is a stable machine-readable identifier."""

    def __init__(self, code: str, *, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail  # internal diagnostics only; must never contain document content

    def __str__(self) -> str:
        return self.code


class DomainError(LdaError):
    """A business rule or invariant was violated."""


class ApplicationError(LdaError):
    """A use case could not be completed as requested."""


class InfrastructureError(LdaError):
    """A technical dependency failed."""


class ValidationError(ApplicationError):
    """Input is structurally or semantically invalid."""


class NotFoundError(ApplicationError):
    """The addressed resource does not exist (or is not visible to the caller)."""


class ConflictError(ApplicationError):
    """The request conflicts with current state."""


class AuthorizationError(ApplicationError):
    """The caller may not perform the action."""


class ExternalProviderError(InfrastructureError):
    """A third-party provider (storage, LLM, OCR, ...) failed or is unavailable."""


class ConfigurationError(InfrastructureError):
    """Configuration is missing or invalid. Never includes values (they may be secrets)."""
