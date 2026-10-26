"""The single egress use case: the only code that reaches the `LlmProvider` port (ADR-006)."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta

from lda.modules.ai_gateway.application.dto import (
    CallEvidence,
    ContextSegment,
    GenerateTextCommand,
    GenerationResult,
)
from lda.modules.ai_gateway.application.policy import EgressPolicy
from lda.modules.ai_gateway.application.ports import (
    LlmProvider,
    ProviderRequest,
    ProviderResponse,
)
from lda.modules.ai_gateway.domain import codes
from lda.modules.ai_gateway.domain.canonical import request_sha256
from lda.modules.ai_gateway.domain.failures import ProviderFailure, RateLimited
from lda.modules.ai_gateway.domain.parameters import GenerationParameters
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.declarations import IDENTIFIER_PATTERN, not_audited, public
from lda.shared.kernel.errors import AuthorizationError, ValidationError
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofExpectation, ProofResource, ProofVerifier
from lda.shared.ports.rate_limiter import RateLimiter, RateLimitKey

RATE_BUCKET = "ai_gateway:egress"
MAX_TOKEN_CHARACTERS = 2048  # the verifier rejects longer proofs; bound it before it is parsed
_ACTION = re.compile(IDENTIFIER_PATTERN)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_RESOURCE_KIND = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_RESOURCE_ID = 256


@public(
    reason="Authorization is the proof, verified through ProofVerifier before every provider call."
)
@not_audited(
    reason="Egress auditing is deferred (ADR-019 D3, ADR-006); returns content-free call evidence "
    "for the later provenance record."
)
class GenerateText:
    """Validate -> derive the exact resource set -> verify the proof -> rate limit -> provider.

    Order is a security property. Nothing reaches the provider before `ProofVerifier.verify`
    returns; a rejected request makes zero provider calls. Verification and rate limiting run
    again before every retry, so a proof revoked or expired during a backoff stops the retry.
    Only `UNREACHABLE` failures (nothing was sent) are retried; the gateway never claims
    exactly-once execution. The completion is returned `Sensitive`; nothing is persisted, cached
    or logged here.
    """

    def __init__(
        self,
        verifier: ProofVerifier,
        provider: LlmProvider,
        rate_limiter: RateLimiter,
        policy: EgressPolicy,
        sleep: Callable[[timedelta], None],
    ) -> None:
        self._verifier = verifier
        self._provider = provider
        self._rate_limiter = rate_limiter
        self._policy = policy
        self._sleep = sleep

    def execute(self, ctx: TenantContext, command: GenerateTextCommand) -> GenerationResult:
        if not isinstance(ctx, TenantContext):
            raise ValidationError(codes.INVALID_REQUEST)
        self._validate(command)  # 1. shape, types, sizes
        resources = frozenset(  # 2. the exact set the proof must bind, derived from the segments
            ProofResource(s.resource.kind, s.resource.id) for s in command.segments
        )
        if not resources:  # never a proof for an empty scope: fail closed without asking
            raise AuthorizationError(codes.SCOPE_EMPTY)
        expected = ProofExpectation(
            tenant_id=ctx.tenant_id,
            principal_id=ctx.principal_id,
            action=command.action,
            resources=resources,
        )
        request = ProviderRequest(
            model=command.model,
            instructions=command.instructions,
            context=tuple(s.text for s in command.segments),
            parameters=command.parameters,
            timeout=self._policy.provider_timeout,
        )
        response, attempts = self._call_with_retries(ctx, command, expected, request)
        evidence = CallEvidence(
            provider_id=self._provider.provider_id,
            model=command.model,
            parameters=command.parameters,
            request_sha256=request_sha256(
                provider_id=self._provider.provider_id,
                model=command.model,
                parameters=command.parameters,
                instructions=command.instructions.reveal(),
                segments=[
                    (s.resource.kind, s.resource.id, s.text.reveal()) for s in command.segments
                ],
            ),
            resources=tuple(sorted(resources, key=lambda r: (r.kind, r.id))),
            attempts=attempts,
        )
        return GenerationResult(Sensitive(response.text.reveal()), evidence)

    def _call_with_retries(
        self,
        ctx: TenantContext,
        command: GenerateTextCommand,
        expected: ProofExpectation,
        request: ProviderRequest,
    ) -> tuple[ProviderResponse, int]:
        attempt = 0
        while True:
            attempt += 1
            # 3-4. Raises AuthorizationError (proof_*) or an infrastructure error: no provider call.
            self._verifier.verify(command.proof_token.reveal(), expected)
            self._acquire(ctx)  # 5. tenant-qualified budget, per provider call
            try:
                return self._provider.generate(request), attempt  # 6.
            except ProviderFailure as failure:
                if not failure.kind.retryable or attempt >= self._policy.max_attempts:
                    # A fresh error: the adapter's exception (and its context) is not chained.
                    raise ProviderFailure(failure.kind) from None
            self._sleep(self._policy.delay_before_retry(attempt))

    def _acquire(self, ctx: TenantContext) -> None:
        decision = self._rate_limiter.acquire(
            RateLimitKey(ctx.tenant_id, RATE_BUCKET), self._policy.rate
        )
        if not decision.allowed:
            raise RateLimited(decision.retry_after.total_seconds())

    def _validate(self, command: GenerateTextCommand) -> None:
        policy = self._policy
        if not isinstance(command, GenerateTextCommand):
            raise ValidationError(codes.INVALID_REQUEST)
        token = _text(command.proof_token)
        if not token or len(token) > MAX_TOKEN_CHARACTERS:
            raise ValidationError(codes.INVALID_REQUEST)
        if not isinstance(command.action, str) or not _ACTION.fullmatch(command.action):
            raise ValidationError(codes.INVALID_REQUEST)
        if not isinstance(command.model, str) or not _MODEL.fullmatch(command.model):
            raise ValidationError(codes.INVALID_REQUEST)
        instructions = _text(command.instructions)
        if not instructions.strip():
            raise ValidationError(codes.INVALID_REQUEST)
        if len(instructions) > policy.max_instruction_characters:
            raise ValidationError(codes.LIMIT_EXCEEDED)
        parameters = command.parameters
        if not isinstance(parameters, GenerationParameters):
            raise ValidationError(codes.INVALID_REQUEST)
        if parameters.max_output_tokens > policy.max_output_tokens:
            raise ValidationError(codes.LIMIT_EXCEEDED)
        if not isinstance(command.segments, tuple):
            raise ValidationError(codes.INVALID_REQUEST)
        if len(command.segments) > policy.max_segments:
            raise ValidationError(codes.LIMIT_EXCEEDED)
        total = len(instructions)
        for segment in command.segments:
            if not isinstance(segment, ContextSegment):
                raise ValidationError(codes.INVALID_REQUEST)
            resource = segment.resource
            if (
                type(resource) is not ProofResource
                or not isinstance(resource.kind, str)
                or not isinstance(resource.id, str)
                or not _RESOURCE_KIND.fullmatch(resource.kind)
                or not 1 <= len(resource.id) <= _MAX_RESOURCE_ID
                or not resource.id.isprintable()
            ):
                raise ValidationError(codes.INVALID_REQUEST)
            text = _text(segment.text)
            if not text:
                raise ValidationError(codes.INVALID_REQUEST)
            if len(text) > policy.max_segment_characters:
                raise ValidationError(codes.LIMIT_EXCEEDED)
            total += len(text)
            if total > policy.max_request_characters:
                raise ValidationError(codes.LIMIT_EXCEEDED)


def _text(value: object) -> str:
    """The wrapped string of a `Sensitive[str]`; anything else is an invalid request."""
    if not isinstance(value, Sensitive):
        raise ValidationError(codes.INVALID_REQUEST)
    inner = value.reveal()
    if not isinstance(inner, str):
        raise ValidationError(codes.INVALID_REQUEST)
    return inner
