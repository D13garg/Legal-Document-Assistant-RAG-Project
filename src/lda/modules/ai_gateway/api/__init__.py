"""Public surface of ai_gateway (ADR-006): the one egress use case and its request/result shapes.

Provider adapters, the retry loop and the canonical form stay private. Callers never pass tenant,
principal or authorization claims: they come from the execution context and the proof.
"""

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
from lda.modules.ai_gateway.application.use_cases.generate_text import GenerateText
from lda.modules.ai_gateway.domain.codes import (
    INVALID_REQUEST,
    LIMIT_EXCEEDED,
    PROVIDER_AMBIGUOUS,
    PROVIDER_REJECTED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNREACHABLE,
    RATE_LIMITED,
    SCOPE_EMPTY,
)
from lda.modules.ai_gateway.domain.failures import FailureKind, ProviderFailure, RateLimited
from lda.modules.ai_gateway.domain.parameters import GenerationParameters

__all__ = [
    "INVALID_REQUEST",
    "LIMIT_EXCEEDED",
    "PROVIDER_AMBIGUOUS",
    "PROVIDER_REJECTED",
    "PROVIDER_TIMEOUT",
    "PROVIDER_UNREACHABLE",
    "RATE_LIMITED",
    "SCOPE_EMPTY",
    "CallEvidence",
    "ContextSegment",
    "EgressPolicy",
    "FailureKind",
    "GenerateText",
    "GenerateTextCommand",
    "GenerationParameters",
    "GenerationResult",
    "LlmProvider",
    "ProviderFailure",
    "ProviderRequest",
    "ProviderResponse",
    "RateLimited",
]
