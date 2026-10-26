"""Contract suite every `LlmProvider` adapter must pass. Run here against the deterministic fake;
a later adapter subclasses `LlmProviderContract` and supplies `make_provider` (ADR-006)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lda.modules.ai_gateway.api import (
    FailureKind,
    GenerationParameters,
    LlmProvider,
    ProviderFailure,
    ProviderRequest,
)
from lda.modules.ai_gateway.testing import FakeLlmProvider
from lda.shared.kernel.sensitive import Sensitive


def request(
    question: str = "q", context: tuple[str, ...] = ("c1",), timeout: float = 5.0
) -> ProviderRequest:
    return ProviderRequest(
        model="m1",
        instructions=Sensitive(question),
        context=tuple(Sensitive(c) for c in context),
        parameters=GenerationParameters(128, 0.0),
        timeout=timedelta(seconds=timeout),
    )


class LlmProviderContract:
    def make_provider(self) -> LlmProvider:
        raise NotImplementedError

    def make_slow_provider(self) -> LlmProvider:
        """A provider whose call takes longer than any sensible timeout (for the timeout rule)."""
        raise NotImplementedError

    def test_the_identifier_is_stable_and_lowercase(self) -> None:
        provider = self.make_provider()
        assert provider.provider_id == provider.provider_id
        assert provider.provider_id and provider.provider_id == provider.provider_id.lower()

    def test_a_response_is_sensitive_non_empty_text(self) -> None:
        text = self.make_provider().generate(request()).text
        assert isinstance(text, Sensitive) and text.reveal()
        assert "c1" not in repr(text) and "c1" not in str(text)

    def test_the_same_request_gives_the_same_completion(self) -> None:
        provider = self.make_provider()
        assert provider.generate(request()).text == provider.generate(request()).text

    def test_different_content_gives_different_completions(self) -> None:
        provider = self.make_provider()
        base = provider.generate(request()).text
        assert provider.generate(request(question="other")).text != base
        assert provider.generate(request(context=("c2",))).text != base
        assert provider.generate(request(context=("c1", "c2"))).text != base

    def test_exceeding_the_timeout_raises_a_timeout_failure(self) -> None:
        with pytest.raises(ProviderFailure) as raised:
            self.make_slow_provider().generate(request(timeout=1.0))
        assert raised.value.kind is FailureKind.TIMEOUT

    def test_the_request_has_no_tools_field(self) -> None:
        fields = set(ProviderRequest.__dataclass_fields__)
        assert fields == {"model", "instructions", "context", "parameters", "timeout"}


class TestFakeLlmProvider(LlmProviderContract):
    def make_provider(self) -> LlmProvider:
        return FakeLlmProvider()

    def make_slow_provider(self) -> LlmProvider:
        return FakeLlmProvider(latency=timedelta(seconds=60))

    def test_scripted_failures_are_consumed_one_per_call_in_order(self) -> None:
        provider = FakeLlmProvider()
        provider.fail_next(FailureKind.UNREACHABLE, FailureKind.REJECTED)
        kinds = []
        for _ in range(2):
            with pytest.raises(ProviderFailure) as raised:
                provider.generate(request())
            kinds.append(raised.value.kind)
        assert kinds == [FailureKind.UNREACHABLE, FailureKind.REJECTED]
        assert provider.generate(request()).text.reveal()
        assert provider.call_count == 3
