"""Test rig for the egress use case: fakes only, no network, no database."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from lda.modules.ai_gateway.api import (
    ContextSegment,
    EgressPolicy,
    GenerateText,
    GenerateTextCommand,
    GenerationParameters,
    ProviderRequest,
    ProviderResponse,
)
from lda.modules.ai_gateway.testing import FakeLlmProvider, FakeProofVerifier
from lda.platform.ratelimit.memory import InMemoryRateLimiter
from lda.platform.testing.doubles import FakeClock
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofExpectation, ProofResource
from lda.shared.ports.rate_limiter import RateLimitPolicy

TENANT_A = TenantId(UUID(int=1))
TENANT_B = TenantId(UUID(int=2))
PRINCIPAL = PrincipalId(UUID(int=11))
ACTION = "grounded_generation.generate_answer"
TOKEN = "v1.primary.payload.mac"  # noqa: S105 - a fixture, not a credential
SECRET_TEXT = "PRIVILEGED-CLIENT-MEMO-7781"  # noqa: S105 - marker text, not a credential
SECRET_QUESTION = "SECRET-QUESTION-4402"  # noqa: S105 - marker text, not a credential
D1, D2 = ProofResource("document", "d1"), ProofResource("document", "d2")


def policy(**overrides: object) -> EgressPolicy:
    base: dict[str, object] = {
        "provider_timeout": timedelta(seconds=10),
        "max_attempts": 3,
        "backoff": timedelta(milliseconds=100),
        "backoff_cap": timedelta(milliseconds=250),
        "rate": RateLimitPolicy(capacity=5, refill_per_second=1.0),
        "max_segments": 5,
        "max_segment_characters": 100,
        "max_request_characters": 300,
        "max_instruction_characters": 100,
        "max_output_tokens": 2048,
    }
    base.update(overrides)
    return EgressPolicy(**base)  # type: ignore[arg-type]


def ctx(tenant: TenantId = TENANT_A) -> TenantContext:
    return TenantContext(tenant, PRINCIPAL, RequestId(UUID(int=99)))


def command(*resources: ProofResource, **overrides: Any) -> GenerateTextCommand:
    token: str = overrides.pop("token", TOKEN)
    chosen = resources or (D1, D2)
    fields: dict[str, object] = {
        "proof_token": Sensitive(token),
        "action": ACTION,
        "model": "fake-model-1",
        "instructions": Sensitive(SECRET_QUESTION),
        "segments": tuple(ContextSegment(r, Sensitive(f"{SECRET_TEXT}-{r.id}")) for r in chosen),
        "parameters": GenerationParameters(max_output_tokens=256, temperature=0.2),
    }
    fields.update(overrides)
    return GenerateTextCommand(**fields)  # type: ignore[arg-type]


def expectation(
    *resources: ProofResource, tenant: TenantId = TENANT_A, action: str = ACTION
) -> ProofExpectation:
    return ProofExpectation(
        tenant_id=tenant,
        principal_id=PRINCIPAL,
        action=action,
        resources=frozenset(resources or (D1, D2)),
    )


@dataclass
class Rig:
    events: list[str] = field(default_factory=list)
    sleeps: list[timedelta] = field(default_factory=list)
    clock: FakeClock = field(default_factory=FakeClock)
    egress_policy: EgressPolicy = field(default_factory=policy)

    def __post_init__(self) -> None:
        self.verifier = FakeProofVerifier(self.events)
        self.provider = RecordingProvider(self.events)
        self.limiter = InMemoryRateLimiter(self.clock)
        self.use_case = GenerateText(
            self.verifier, self.provider, self.limiter, self.egress_policy, self.sleeps.append
        )

    def allow(self, *resources: ProofResource, tenant: TenantId = TENANT_A) -> None:
        self.verifier.allow(TOKEN, expectation(*resources, tenant=tenant))


class RecordingProvider(FakeLlmProvider):
    """The fake provider plus an ordering log shared with the verifier."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self._events.append("provider")
        return super().generate(request)
