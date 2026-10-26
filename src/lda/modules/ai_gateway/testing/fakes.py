"""Deterministic fakes for tests of this module and of modules that call the gateway.

Never imported by production code. The fake provider makes no network call, holds no credential and
needs no SDK (ADR-006 decision: fake-only in Step 9).
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from lda.modules.ai_gateway.application.ports import ProviderRequest, ProviderResponse
from lda.modules.ai_gateway.domain.failures import FailureKind, ProviderFailure
from lda.shared.kernel.errors import AuthorizationError
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofExpectation


class FakeLlmProvider:
    """Completion is a pure function of the request, so tests can assert exact output.

    `fail_next(...)` queues failures consumed one per call; `latency` simulates a slow provider and
    raises TIMEOUT when it exceeds the request's timeout, like a real adapter must.
    """

    def __init__(self, provider_id: str = "fake", latency: timedelta = timedelta(0)) -> None:
        self._provider_id = provider_id
        self.latency = latency
        self.requests: list[ProviderRequest] = []
        self._failures: list[FailureKind] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def fail_next(self, *kinds: FailureKind) -> None:
        self._failures.extend(kinds)

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if self._failures:
            raise ProviderFailure(self._failures.pop(0))
        if self.latency > request.timeout:
            raise ProviderFailure(FailureKind.TIMEOUT)
        digest = hashlib.sha256()
        for part in (request.model, request.instructions.reveal(), *map(_reveal, request.context)):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x00")
        digest.update(
            f"{request.parameters.max_output_tokens}:{request.parameters.temperature}".encode()
        )
        return ProviderResponse(Sensitive(f"fake-completion:{digest.hexdigest()[:16]}"))


def _reveal(value: Sensitive[str]) -> str:
    return value.reveal()


class FakeProofVerifier:
    """Accepts exactly the `(token, expectation)` pairs it was told to; rejects everything else
    with a `proof_*` code. Records every call and appends `verify` to a shared event log so tests
    can assert ordering against the provider."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.calls: list[tuple[str, ProofExpectation]] = []
        self._valid: set[tuple[str, ProofExpectation]] = set()
        self._reject_codes: list[str | None] = []

    def allow(self, token: str, expected: ProofExpectation) -> None:
        self._valid.add((token, expected))

    def reject_next(self, *codes: str | None) -> None:
        """Script the next verifications: a code rejects with it (even for an allowed token), `None`
        defers to the allow-list."""
        self._reject_codes.extend(codes)

    def verify(self, token: str, expected: ProofExpectation) -> None:
        self.events.append("verify")
        self.calls.append((token, expected))
        scripted = self._reject_codes.pop(0) if self._reject_codes else None
        if scripted is not None:
            raise AuthorizationError(scripted)
        if (token, expected) not in self._valid:
            raise AuthorizationError("proof_mismatch")
