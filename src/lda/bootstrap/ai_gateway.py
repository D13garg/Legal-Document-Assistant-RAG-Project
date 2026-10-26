"""AI gateway wiring (ADR-006): the only place that joins the egress use case to its provider, the
proof verifier and the rate limiter.

`ai_gateway` never imports `access_control`: the verifier arrives typed as the shared
`ProofVerifier` port (`build_access_control(...).proof_verifier`). No real provider adapter exists
yet, so the caller supplies the `LlmProvider` (a fake in tests); a later step adds adapters under
`ai_gateway/infrastructure/adapters` and chooses one here. Nothing is persisted: no migrations.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from lda.bootstrap.container import PlatformContainer
from lda.modules.ai_gateway.api import (
    EgressPolicy,
    FailureKind,
    GenerateText,
    LlmProvider,
    ProviderFailure,
    ProviderRequest,
    ProviderResponse,
)
from lda.modules.ai_gateway.config import AiGatewaySettings
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofVerifier
from lda.shared.ports.rate_limiter import RateLimitPolicy


@dataclass(frozen=True)
class AiGatewayContainer:
    generate_text: GenerateText


def load_ai_gateway_settings(config: ConfigurationLoader) -> AiGatewaySettings:
    return config.load("ai_gateway", AiGatewaySettings)


def egress_policy(settings: AiGatewaySettings) -> EgressPolicy:
    return EgressPolicy(
        provider_timeout=timedelta(seconds=settings.provider_timeout_seconds),
        max_attempts=settings.max_attempts,
        backoff=timedelta(seconds=settings.backoff_seconds),
        backoff_cap=timedelta(seconds=settings.backoff_cap_seconds),
        rate=RateLimitPolicy(settings.rate_capacity, settings.rate_refill_per_second),
        max_segments=settings.max_segments,
        max_segment_characters=settings.max_segment_characters,
        max_request_characters=settings.max_request_characters,
        max_instruction_characters=settings.max_instruction_characters,
        max_output_tokens=settings.max_output_tokens,
    )


def build_ai_gateway(
    platform: PlatformContainer,
    verifier: ProofVerifier,
    provider: LlmProvider,
    settings: AiGatewaySettings,
    sleep: Callable[[timedelta], None] | None = None,
) -> AiGatewayContainer:
    pause = sleep if sleep is not None else _sleep
    use_case = GenerateText(
        verifier, provider, platform.rate_limiter, egress_policy(settings), pause
    )
    return AiGatewayContainer(generate_text=use_case)


def _sleep(delay: timedelta) -> None:
    time.sleep(delay.total_seconds())


# ---- DEVELOPMENT-ONLY provider (ADR-045) -----------------------------------------------
# Lives here because this is the one production file the gateway guard lets name the provider
# port. Reached only through `lda.bootstrap.development.development_llm` (guarded). It is not a
# language model and makes no network call.
DEVELOPMENT_MODEL = "development-extractive"  # the only model `DevelopmentLlm` accepts
_WORD = re.compile(r"[a-z0-9]+")
_QUESTION = re.compile(
    r"\[\[LDA:BEGIN block=user_question trust=untrusted\]\]\n(.*?)\n"
    r"\[\[LDA:END block=user_question\]\]",
    re.DOTALL,
)
_PART = re.compile(
    r"\[\[LDA:BEGIN block=document_context part=(\d+)/\d+ trust=untrusted\]\]\n(.*?)\n"
    r"\[\[LDA:END block=document_context part=\d+/\d+\]\]",
    re.DOTALL,
)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "shall",
        "that",
        "the",
        "their",
        "there",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
    ]
)
_MIN_QUOTE, _MAX_QUOTE = 12, 400  # the grounded-generation contract's quote bounds


class DevelopmentLlm:
    """Extractive and deterministic: cites the supplied passage whose sentence best overlaps the
    question, quoting that sentence verbatim; otherwise reports insufficient evidence."""

    @property
    def provider_id(self) -> str:
        return "development-extractive"

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        if request.model != DEVELOPMENT_MODEL:
            raise ProviderFailure(FailureKind.REJECTED)
        found = _QUESTION.search(request.instructions.reveal())
        wanted = set(_WORD.findall((found.group(1) if found else "").lower())) - _STOP
        best: tuple[int, int, str, str] | None = None  # (overlap, -part, sentence, marker)
        for position, block in enumerate(request.context, start=1):
            part = _PART.search(block.reveal())
            if part is None:
                continue
            for sentence in _SENTENCES.split(part.group(2)):
                sentence = sentence.strip()
                if not _MIN_QUOTE <= len(sentence) <= _MAX_QUOTE:
                    continue
                overlap = len(wanted & set(_WORD.findall(sentence.lower())))
                candidate = (overlap, -int(part.group(1)), sentence, f"S{position}")
                if overlap > 0 and (best is None or candidate[:2] > best[:2]):
                    best = candidate
        if best is None:
            body: dict[str, object] = {"outcome": "insufficient_evidence", "statements": []}
        else:
            body = {
                "outcome": "answered",
                "statements": [{"text": best[2], "citation": best[3], "quote": best[2]}],
            }
        return ProviderResponse(Sensitive(json.dumps(body)))


def build_development_provider() -> DevelopmentLlm:
    """Call only through `lda.bootstrap.development`, which requires the development guard."""
    return DevelopmentLlm()


def build_development_gateway(
    platform: PlatformContainer, verifier: ProofVerifier, settings: AiGatewaySettings
) -> AiGatewayContainer:
    """The gateway wired to the DEVELOPMENT provider (ADR-045). Reached only through
    `lda.bootstrap.headless`, which requires the development guard first."""
    return build_ai_gateway(platform, verifier, DevelopmentLlm(), settings)
