from __future__ import annotations

from lda.shared.kernel.errors import ApplicationError


class RetryRequested(ApplicationError):
    """A stage asked for a retry. Raised to the worker, which applies the platform's jittered
    backoff for this attempt; `code` is a stage-supplied identifier, never free text."""
