"""Test-only builders for prompts and its consumers (never imported by production code).

The test-only template is reachable only from here, so the production path cannot see it.
"""

from lda.modules.prompts.domain.catalog.proposed_grounded_answer import (
    PROPOSED_GROUNDED_ANSWER_ID,
    PROPOSED_GROUNDED_ANSWER_VERSION,
)
from lda.modules.prompts.domain.catalog.sample_template import (
    TEST_ONLY_TEMPLATE_ID,
    TEST_ONLY_TEMPLATE_VERSION,
)
from lda.modules.prompts.testing.builders import (
    build_context_input,
    build_prompt_request,
    catalog_with_proposed_grounded_answer,
    catalog_with_test_only_template,
    default_limits,
)

__all__ = [
    "PROPOSED_GROUNDED_ANSWER_ID",
    "PROPOSED_GROUNDED_ANSWER_VERSION",
    "TEST_ONLY_TEMPLATE_ID",
    "TEST_ONLY_TEMPLATE_VERSION",
    "build_context_input",
    "build_prompt_request",
    "catalog_with_proposed_grounded_answer",
    "catalog_with_test_only_template",
    "default_limits",
]
