"""Public surface of prompts (ADR-017): the pure renderer and its neutral input/output shapes.

Prompts is authorization-agnostic. It never imports `access_control` or `ai_gateway`, never builds a
`ContextSegment` and never decides who may see what; the later composition layer maps
`RenderedPrompt` into the gateway command, and the gateway's `ProofVerifier` stays the egress gate.
Template internals, escaping primitives and the test-only template are not public.
"""

from lda.modules.prompts.application.dto import (
    PromptContextInput,
    PromptRequest,
    RenderedContextPart,
    RenderedPrompt,
)
from lda.modules.prompts.application.limits import PromptLimits
from lda.modules.prompts.application.renderer import PromptRenderer
from lda.modules.prompts.domain.catalog import PRODUCTION_TEMPLATES
from lda.modules.prompts.domain.codes import (
    CONTEXT_EMPTY,
    INVALID_INPUT,
    LIMIT_EXCEEDED,
    TEMPLATE_UNKNOWN,
)
from lda.modules.prompts.domain.template import PromptCatalog, PromptTemplate


def production_catalog() -> PromptCatalog:
    """The approved production templates (currently only `grounded_answer` version 1, ADR-029)."""
    return PromptCatalog(PRODUCTION_TEMPLATES, production=True)


__all__ = [
    "CONTEXT_EMPTY",
    "INVALID_INPUT",
    "LIMIT_EXCEEDED",
    "TEMPLATE_UNKNOWN",
    "PromptCatalog",
    "PromptContextInput",
    "PromptLimits",
    "PromptRenderer",
    "PromptRequest",
    "PromptTemplate",
    "RenderedContextPart",
    "RenderedPrompt",
    "production_catalog",
]
