"""Ports of the gateway. `LlmProvider` is the only way text leaves the system (ADR-006)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from lda.modules.ai_gateway.domain.parameters import GenerationParameters
from lda.shared.kernel.sensitive import Sensitive


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """What an adapter receives. There is no tools field and no way to add one: the model has no
    tools (invariant 10)."""

    model: str
    instructions: Sensitive[str]
    context: tuple[Sensitive[str], ...]
    parameters: GenerationParameters
    timeout: timedelta
    """Hard per-attempt limit the adapter must enforce on the network call."""


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: Sensitive[str]


class LlmProvider(Protocol):
    """One text-generation provider. Contract (checked by the shared contract suite):

    - `provider_id` is a stable lowercase identifier;
    - `generate` returns a response or raises `ProviderFailure` (domain.failures) and nothing else
      for provider-side trouble: SDK errors are translated, their messages never copied;
    - it enforces `request.timeout` and raises `ProviderFailure(TIMEOUT)` when exceeded;
    - it raises `UNREACHABLE` only when nothing of the request reached the provider;
    - it does not retry, cache, log or persist request or response content.
    """

    @property
    def provider_id(self) -> str: ...

    def generate(self, request: ProviderRequest) -> ProviderResponse: ...
