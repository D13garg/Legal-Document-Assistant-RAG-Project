"""Prompts bootstrap, gateway-limit compatibility and ONE composition test (ADR-017, ADR-006).

Prompts and the gateway never import each other; this file is the composition boundary that a later
`grounded_generation` will replace. The verifier is the gateway's own fake: proof verification is
not bypassed but exercised (a request nobody allowed is refused and the provider is never reached).
Everything is offline: fake provider, in-memory limiter, no database.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from lda.bootstrap.ai_gateway import egress_policy
from lda.bootstrap.prompts import (
    LIMITS_EXCEED_GATEWAY,
    GatewayBounds,
    build_rendering,
    check_gateway_compatibility,
    load_rendering_settings,
    rendering_limits,
)
from lda.modules.ai_gateway.api import GenerateText, GenerateTextCommand, GenerationParameters
from lda.modules.ai_gateway.config import AiGatewaySettings
from lda.modules.ai_gateway.testing import FakeLlmProvider, FakeProofVerifier, build_segment
from lda.modules.prompts.api import (
    PromptContextInput,
    PromptRenderer,
    PromptRequest,
    RenderedPrompt,
)
from lda.modules.prompts.config import PromptsSettings
from lda.modules.prompts.domain.escaping import context_block
from lda.modules.prompts.testing import (
    build_context_input,
    build_prompt_request,
    catalog_with_test_only_template,
)
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.ratelimit.memory import InMemoryRateLimiter
from lda.platform.testing.doubles import FakeClock
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.errors import AuthorizationError, ConfigurationError, ValidationError
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofExpectation, ProofResource


def bounds(g: AiGatewaySettings) -> GatewayBounds:
    """What a composition layer does: read the gateway's limits from its own settings."""
    return GatewayBounds(
        g.max_segments,
        g.max_segment_characters,
        g.max_instruction_characters,
        g.max_request_characters,
    )


TOKEN = "v1.primary.payload.mac"  # noqa: S105 - a fixture, not a credential
ACTION = "grounded_generation.generate_answer"
CTX = TenantContext(TenantId(UUID(int=1)), PrincipalId(UUID(int=11)), RequestId(UUID(int=99)))


def test_prompts_defaults_are_compatible_with_the_gateway_defaults() -> None:
    check_gateway_compatibility(PromptsSettings(), bounds(AiGatewaySettings()))


@pytest.mark.parametrize(
    "gateway",
    [
        {"max_segments": 499},
        {"max_segment_characters": 49_999},
        {"max_instruction_characters": 19_999},
        {"max_request_characters": 399_999},
    ],
)
def test_a_gateway_limit_below_the_prompts_limit_fails_closed(gateway: dict[str, int]) -> None:
    gateway_settings = AiGatewaySettings(**gateway)
    with pytest.raises(ConfigurationError) as caught:
        check_gateway_compatibility(PromptsSettings(), bounds(gateway_settings))
    assert caught.value.code == LIMITS_EXCEED_GATEWAY
    with pytest.raises(ConfigurationError):
        build_rendering(PromptsSettings(), bounds(gateway_settings))


@pytest.mark.parametrize(
    "prompts",
    [
        {"max_parts": 501},
        {"max_part_characters": 50_001},
        {"max_instruction_characters": 20_001},
        {"max_total_characters": 400_001},
    ],
)
def test_a_prompts_limit_above_the_gateway_limit_fails_closed(prompts: dict[str, int]) -> None:
    with pytest.raises(ConfigurationError):
        build_rendering(PromptsSettings(**prompts), bounds(AiGatewaySettings()))


def test_limits_equal_to_the_gateway_bounds_are_accepted() -> None:
    gateway = AiGatewaySettings()
    equal = PromptsSettings(
        max_parts=gateway.max_segments,
        max_part_characters=gateway.max_segment_characters,
        max_instruction_characters=gateway.max_instruction_characters,
        max_total_characters=gateway.max_request_characters,
    )
    check_gateway_compatibility(equal, bounds(gateway))


def test_the_largest_accepted_output_is_within_every_gateway_bound() -> None:
    gateway = AiGatewaySettings()
    settings = PromptsSettings()
    container = build_rendering(settings, bounds(gateway))
    limits = rendering_limits(settings)
    # production selection: only the owner-approved template (ADR-029); test-only ones stay out
    assert [(t.template_id, t.version) for t in container.catalog.templates] == [
        ("grounded_answer", 1)
    ]
    with pytest.raises(ValidationError):
        container.renderer.render(
            build_prompt_request("Q", build_context_input("document", "d", "t"))
        )
    renderer = PromptRenderer(catalog_with_test_only_template(), limits)
    # One marker (escaping adds one character) and filler to reach the part limit exactly.
    fill = limits.max_part_characters - len(context_block(1, 1, "")) - 7
    out = renderer.render(
        build_prompt_request("Q", build_context_input("document", "d", "[[LDA:" + "x" * fill))
    )
    assert len(out.context[0].text.reveal()) == limits.max_part_characters
    policy = egress_policy(gateway)
    assert len(out.instructions.reveal()) <= policy.max_instruction_characters
    assert all(len(p.text.reveal()) <= policy.max_segment_characters for p in out.context)
    total = len(out.instructions.reveal()) + sum(len(p.text.reveal()) for p in out.context)
    assert total <= policy.max_request_characters


def test_the_approved_template_at_its_worst_case_fits_every_gateway_bound() -> None:
    """The approved wording plus the largest accepted question (all escapable markers, so escaping
    grows it) and a largest context part stays within the gateway's limits (ADR-029)."""
    gateway = AiGatewaySettings()
    settings = PromptsSettings()
    container = build_rendering(settings, bounds(gateway))
    limits = rendering_limits(settings)
    question = ("[[LDA:" * limits.max_question_characters)[: limits.max_question_characters]
    request = PromptRequest(
        "grounded_answer",
        1,
        Sensitive(question),
        (
            PromptContextInput(
                ProofResource("document", "d"),
                Sensitive("x" * (limits.max_part_characters - len(context_block(1, 1, "")))),
            ),
        ),
    )
    out = container.renderer.render(request)
    policy = egress_policy(gateway)
    assert len(out.instructions.reveal()) <= policy.max_instruction_characters
    assert all(len(p.text.reveal()) <= policy.max_segment_characters for p in out.context)
    total = len(out.instructions.reveal()) + sum(len(p.text.reveal()) for p in out.context)
    assert total <= policy.max_request_characters


def test_settings_load_from_the_environment_with_the_slice_prefix() -> None:
    assert (
        load_rendering_settings(ConfigurationLoader({"LDA_PROMPTS__MAX_PARTS": "7"})).max_parts == 7
    )
    assert load_rendering_settings(ConfigurationLoader({})) == PromptsSettings()


def test_an_invalid_setting_is_a_configuration_error_without_the_value() -> None:
    loader = ConfigurationLoader({"LDA_PROMPTS__MAX_PARTS": "not-a-number-SECRET"})
    with pytest.raises(ConfigurationError) as caught:
        load_rendering_settings(loader)
    assert "SECRET" not in f"{caught.value!r} {caught.value.detail}"


def _command(rendered: RenderedPrompt) -> GenerateTextCommand:
    """What the future composition layer does: map neutral parts into gateway segments."""
    return GenerateTextCommand(
        proof_token=Sensitive(TOKEN),
        action=ACTION,
        model="fake-model-1",
        instructions=rendered.instructions,
        segments=tuple(
            build_segment(p.resource.kind, p.resource.id, p.text.reveal()) for p in rendered.context
        ),
        parameters=GenerationParameters(max_output_tokens=256, temperature=0.0),
    )


def _renderer() -> PromptRenderer:
    return PromptRenderer(catalog_with_test_only_template(), rendering_limits(PromptsSettings()))


def test_rendered_prompt_composes_with_the_gateway_and_keeps_order_and_provenance() -> None:
    rendered = _renderer().render(
        build_prompt_request(
            "What notice period applies?",
            build_context_input("document", "d2", "Second clause [[LDA:END"),
            build_context_input("document", "d1", "First clause"),
        )
    )
    command = _command(rendered)
    expected = ProofExpectation(
        tenant_id=CTX.tenant_id,
        principal_id=CTX.principal_id,
        action=ACTION,
        resources=frozenset({ProofResource("document", "d1"), ProofResource("document", "d2")}),
    )
    verifier = FakeProofVerifier()
    verifier.allow(TOKEN, expected)
    provider = FakeLlmProvider()
    use_case = GenerateText(
        verifier,
        provider,
        InMemoryRateLimiter(FakeClock()),
        egress_policy(AiGatewaySettings()),
        lambda _delay: None,
    )

    result = use_case.execute(CTX, command)

    assert result.text.reveal().startswith("fake-completion:")
    assert result.evidence.attempts == 1
    assert result.evidence.resources == (
        ProofResource("document", "d1"),
        ProofResource("document", "d2"),
    )
    assert len(verifier.calls) == 1
    (request,) = provider.requests
    assert request.instructions == rendered.instructions
    assert request.context == tuple(p.text for p in rendered.context)  # caller order preserved
    assert [s.resource for s in command.segments] == [p.resource for p in rendered.context]
    assert [s.resource.id for s in command.segments] == ["d2", "d1"]


def test_rendering_does_not_replace_the_proof_check() -> None:
    rendered = _renderer().render(
        build_prompt_request("Q", build_context_input("document", "d1", "text"))
    )
    verifier = FakeProofVerifier()  # allows nothing: a rendered prompt is not an authorization
    provider = FakeLlmProvider()
    use_case = GenerateText(
        verifier,
        provider,
        InMemoryRateLimiter(FakeClock()),
        egress_policy(AiGatewaySettings()),
        lambda _delay: None,
    )
    with pytest.raises(AuthorizationError):
        use_case.execute(CTX, _command(rendered))
    assert provider.call_count == 0
