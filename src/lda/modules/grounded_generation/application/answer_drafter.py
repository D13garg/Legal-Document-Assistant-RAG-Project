"""`AnswerDrafter`: render the prompt, re-authorize, then make the one gateway call (ADR-029).

The evidence is already authorized and verified. The prompt is built from it with the renderer
(trusted instructions; question and passages are untrusted blocks), and then a SECOND fresh
decision is taken immediately before egress, so a grant revoked while the prompt was being built
stops the call. The gateway verifies that proof again before every provider call. A failure at any
step sends nothing; nothing here is retried, cached or logged, and no provider output is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.ai_gateway.api import (
    CallEvidence,
    ContextSegment,
    GenerateText,
    GenerateTextCommand,
)
from lda.modules.citations.api import Citation, TextEvidence, proof_resource
from lda.modules.documents.api import READ_DOCUMENT
from lda.modules.grounded_generation.application.policy import GroundingPolicy
from lda.modules.grounded_generation.application.ports import DocumentAccess
from lda.modules.grounded_generation.domain import codes
from lda.modules.prompts.api import (
    INVALID_INPUT as PROMPT_INVALID_INPUT,
)
from lda.modules.prompts.api import (
    LIMIT_EXCEEDED as PROMPT_LIMIT_EXCEEDED,
)
from lda.modules.prompts.api import (
    TEMPLATE_UNKNOWN as PROMPT_TEMPLATE_UNKNOWN,
)
from lda.modules.prompts.api import (
    PromptContextInput,
    PromptRenderer,
    PromptRequest,
    RenderedPrompt,
)
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.errors import AuthorizationError, ConfigurationError, ValidationError
from lda.shared.kernel.sensitive import Sensitive


@dataclass(frozen=True, slots=True)
class Draft:
    """The provider's untrusted reply and the content-free evidence of the call that made it."""

    reply: Sensitive[str]
    call: CallEvidence


class AnswerDrafter:
    def __init__(
        self,
        access: DocumentAccess,
        renderer: PromptRenderer,
        gateway: GenerateText,
        policy: GroundingPolicy,
    ) -> None:
        self._access = access
        self._renderer = renderer
        self._gateway = gateway
        self._policy = policy

    def draft(self, ctx: TenantContext, question: Sensitive[str], evidence: TextEvidence) -> Draft:
        owned = evidence.evidence
        if (owned.tenant_id, owned.principal_id) != (ctx.tenant_id, ctx.principal_id):
            raise AuthorizationError(codes.NOT_AUTHORIZED)  # evidence of another generation
        prompt = self._render(question, evidence)
        documents = frozenset(c.source.document_id for c in owned.citations)
        token = self._access.authorize_read(ctx, documents)  # fresh, right before egress
        result = self._gateway.execute(
            ctx,
            GenerateTextCommand(
                proof_token=token,
                action=READ_DOCUMENT,
                model=self._policy.model,
                instructions=prompt.instructions,
                segments=tuple(ContextSegment(p.resource, p.text) for p in prompt.context),
                parameters=self._policy.parameters,
            ),
        )
        return Draft(result.text, result.evidence)

    def confirm_access(self, ctx: TenantContext, citations: tuple[Citation, ...]) -> None:
        """A last fresh decision before chunk-derived statements are released to the caller.

        Generation takes time; a grant revoked meanwhile must not still yield quoted text. Raises
        `AuthorizationError` when the caller no longer holds the action on every cited document.
        The token is discarded: it authorizes nothing further.
        """
        self._access.authorize_read(ctx, frozenset(c.source.document_id for c in citations))

    def _render(self, question: Sensitive[str], evidence: TextEvidence) -> RenderedPrompt:
        context = tuple(
            PromptContextInput(proof_resource(c.source.document_id), Sensitive(p.text))
            for c, p in zip(evidence.evidence.citations, evidence.passages, strict=True)
        )
        try:
            return self._renderer.render(
                PromptRequest(
                    self._policy.template_id, self._policy.template_version, question, context
                )
            )
        except ValidationError as refused:
            if refused.code == PROMPT_TEMPLATE_UNKNOWN:  # nothing approved is configured
                raise ConfigurationError(codes.TEMPLATE_UNAVAILABLE) from None
            if refused.code == PROMPT_LIMIT_EXCEEDED:
                raise ValidationError(codes.LIMIT_EXCEEDED, detail=refused.detail) from None
            raise ValidationError(
                codes.INVALID_INPUT,
                detail=refused.detail if refused.code == PROMPT_INVALID_INPUT else "",
            ) from None
