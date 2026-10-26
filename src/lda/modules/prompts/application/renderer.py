"""The pure renderer (ADR-017): template + untrusted question + ordered context -> `RenderedPrompt`.

Not a use case: it reads nothing, writes nothing, emits nothing and needs no tenant. It never
grants access, mints proofs or builds gateway segments. Order is preserved exactly; nothing is
truncated, sorted, merged, stripped or dropped; invalid or oversized input fails closed with a
content-free `ValidationError`. The same template version and input always give identical output.
"""

from __future__ import annotations

import re

from lda.modules.prompts.application.dto import (
    PromptContextInput,
    PromptRequest,
    RenderedContextPart,
    RenderedPrompt,
)
from lda.modules.prompts.application.limits import PromptLimits
from lda.modules.prompts.domain import codes
from lda.modules.prompts.domain.escaping import context_block, question_block
from lda.modules.prompts.domain.template import PromptCatalog
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofResource

_RESOURCE_KIND = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_RESOURCE_ID = 256


class PromptRenderer:
    def __init__(self, catalog: PromptCatalog, limits: PromptLimits) -> None:
        self._catalog = catalog
        self._limits = limits

    def render(self, request: PromptRequest) -> RenderedPrompt:
        limits = self._limits
        if not isinstance(request, PromptRequest):
            raise _invalid("request")
        template = self._catalog.get(request.template_id, request.template_version)
        question = _text(request.question, "question")
        if not question.strip():
            raise _invalid("question")
        if not isinstance(request.context, tuple):
            raise _invalid("context")
        if not request.context:
            raise ValidationError(codes.CONTEXT_EMPTY, detail="context")
        if len(request.context) > limits.max_parts:
            raise _limit("max_parts")
        resources, texts = self._read_context(request.context)
        # Raw length is a lower bound of rendered length: refuse before doing any escaping work.
        if len(question) > limits.max_question_characters:
            raise _limit("max_question_characters")
        if sum(map(len, texts)) > limits.max_total_characters:
            raise _limit("max_total_characters")
        instructions = template.instructions + "\n\n" + question_block(question)
        if len(instructions) > limits.max_instruction_characters:
            raise _limit("max_instruction_characters")
        total = len(instructions)
        parts: list[RenderedContextPart] = []
        count = len(texts)
        for index, (resource, text) in enumerate(zip(resources, texts, strict=True), start=1):
            rendered = context_block(index, count, text)
            if len(rendered) > limits.max_part_characters:
                raise _limit("max_part_characters")
            total += len(rendered)
            if total > limits.max_total_characters:
                raise _limit("max_total_characters")
            parts.append(
                RenderedContextPart(ProofResource(resource.kind, resource.id), _wrap(rendered))
            )
        return RenderedPrompt(
            template_id=template.template_id,
            template_version=template.version,
            instructions=_wrap(instructions),
            context=tuple(parts),
        )

    def _read_context(
        self, items: tuple[PromptContextInput, ...]
    ) -> tuple[list[ProofResource], list[str]]:
        """Validate every part and read each `Sensitive` exactly once (no second look at a value a
        subclass could change)."""
        resources: list[ProofResource] = []
        texts: list[str] = []
        for item in items:
            if not isinstance(item, PromptContextInput):
                raise _invalid("context_item")
            resource = item.resource
            if (
                type(resource) is not ProofResource
                or type(resource.kind) is not str
                or type(resource.id) is not str
                or not _RESOURCE_KIND.fullmatch(resource.kind)
                or not 1 <= len(resource.id) <= _MAX_RESOURCE_ID
                or not resource.id.isprintable()
            ):
                raise _invalid("resource")
            text = _text(item.text, "context_text")
            if not text.strip():
                raise _invalid("context_text")
            if len(text) > self._limits.max_part_characters:
                raise _limit("max_part_characters")
            resources.append(resource)
            texts.append(text)
        return resources, texts


def _wrap(value: str) -> Sensitive[str]:
    return Sensitive(value)


def _text(value: object, field: str) -> str:
    """The wrapped string of a `Sensitive[str]`; anything else, a lone surrogate or NUL is invalid.
    The offending value is never placed in the error."""
    if not isinstance(value, Sensitive):
        raise _invalid(field)
    inner = value.reveal()
    if type(inner) is not str or "\x00" in inner:
        raise _invalid(field)
    if not inner.isascii():
        try:
            inner.encode("utf-8")
        except UnicodeEncodeError:
            raise _invalid(field) from None
    return inner


def _invalid(field: str) -> ValidationError:
    return ValidationError(codes.INVALID_INPUT, detail=field)


def _limit(name: str) -> ValidationError:
    return ValidationError(codes.LIMIT_EXCEEDED, detail=name)
