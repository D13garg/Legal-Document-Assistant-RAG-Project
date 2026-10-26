"""Builders for the test-only template, requests and limits. Not production code."""

from __future__ import annotations

from lda.modules.prompts.application.dto import PromptContextInput, PromptRequest
from lda.modules.prompts.application.limits import PromptLimits
from lda.modules.prompts.domain.catalog.proposed_grounded_answer import PROPOSED_GROUNDED_ANSWER
from lda.modules.prompts.domain.catalog.sample_template import (
    TEST_ONLY_TEMPLATE,
    TEST_ONLY_TEMPLATE_ID,
    TEST_ONLY_TEMPLATE_VERSION,
)
from lda.modules.prompts.domain.template import PromptCatalog
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofResource


def catalog_with_test_only_template() -> PromptCatalog:
    """A NON-production catalog holding only the test-only template."""
    return PromptCatalog((TEST_ONLY_TEMPLATE,), production=False)


def catalog_with_proposed_grounded_answer() -> PromptCatalog:
    """A NON-production catalog holding the approved `grounded_answer` v1 template (ADR-029).

    For tests of grounded generation: the real wording and the real answer contract are exercised
    together. The production catalog serves the same template (`prompts.api.production_catalog`)."""
    return PromptCatalog((PROPOSED_GROUNDED_ANSWER,), production=False)


def build_context_input(kind: str, resource_id: str, text: str) -> PromptContextInput:
    return PromptContextInput(ProofResource(kind, resource_id), Sensitive(text))


def build_prompt_request(
    question: str,
    *context: PromptContextInput,
    template_id: str = TEST_ONLY_TEMPLATE_ID,
    template_version: int = TEST_ONLY_TEMPLATE_VERSION,
) -> PromptRequest:
    return PromptRequest(template_id, template_version, Sensitive(question), tuple(context))


def default_limits() -> PromptLimits:
    """Limits equal to the slice defaults (a test pins the equality; `testing` may not import the
    config slice)."""
    return PromptLimits(
        max_parts=500,
        max_part_characters=50_000,
        max_instruction_characters=20_000,
        max_question_characters=8_000,
        max_total_characters=400_000,
    )
