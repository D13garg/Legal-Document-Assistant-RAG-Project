"""Rendering wiring for the `prompts` module (ADR-017): joins the pure renderer to its settings and
checks them against the gateway's limits.

`prompts` never imports `ai_gateway`, `access_control` or `bootstrap`, and (frozen Step 9 contract)
only `bootstrap/ai_gateway.py` may import the gateway, so the gateway's limits arrive here as plain
integers (`GatewayBounds`) that the composition layer reads from its own gateway settings. Frozen
Step 1 keeps domain vocabulary out of bootstrap names, hence `rendering` in function and class
names. Nothing is persisted (`owns_schema = ""`), cached or tenant-dependent. The container
holds the
production catalog, which serves exactly the owner-approved `grounded_answer` v1 (ADR-029 D8); any
other selection fails with `prompt_template_unknown`.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.prompts.api import PromptCatalog, PromptLimits, PromptRenderer, production_catalog
from lda.modules.prompts.config import PromptsSettings
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError

LIMITS_EXCEED_GATEWAY = "prompts_limits_exceed_gateway"


@dataclass(frozen=True)
class GatewayBounds:
    """The gateway's accepted maxima (`max_segments`, `max_segment_characters`,
    `max_instruction_characters`, `max_request_characters`), read by the caller."""

    max_segments: int
    max_segment_characters: int
    max_instruction_characters: int
    max_request_characters: int


@dataclass(frozen=True)
class RenderingContainer:
    renderer: PromptRenderer
    catalog: PromptCatalog


def load_rendering_settings(config: ConfigurationLoader) -> PromptsSettings:
    return config.load("prompts", PromptsSettings)


def rendering_limits(settings: PromptsSettings) -> PromptLimits:
    return PromptLimits(
        max_parts=settings.max_parts,
        max_part_characters=settings.max_part_characters,
        max_instruction_characters=settings.max_instruction_characters,
        max_question_characters=settings.max_question_characters,
        max_total_characters=settings.max_total_characters,
    )


def check_gateway_compatibility(settings: PromptsSettings, gateway: GatewayBounds) -> None:
    """Fail closed when a rendered prompt within the renderer limits could be rejected by the
    gateway. The gateway counts instructions and context together against its request limit."""
    if (
        settings.max_parts > gateway.max_segments
        or settings.max_part_characters > gateway.max_segment_characters
        or settings.max_instruction_characters > gateway.max_instruction_characters
        or settings.max_total_characters > gateway.max_request_characters
    ):
        raise ConfigurationError(LIMITS_EXCEED_GATEWAY)


def build_rendering(settings: PromptsSettings, gateway: GatewayBounds) -> RenderingContainer:
    check_gateway_compatibility(settings, gateway)
    catalog = production_catalog()
    return RenderingContainer(PromptRenderer(catalog, rendering_limits(settings)), catalog)
