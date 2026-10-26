"""The use case of answering (ADR-040): one question, one linked, grounded turn."""

from __future__ import annotations

from uuid import UUID

from lda.modules.access_control.api import AccessScope, ActionId, AuthorizationRequest, ResourceRef
from lda.modules.answering.application.dto import AnswerCommand, AnsweredTurn
from lda.modules.answering.application.limits import AnswerLimits
from lda.modules.answering.application.ports import (
    AnswerGenerator,
    AssistantTurnWriter,
    ChunkSearch,
    ScopeResolver,
    UserMessageWriter,
)
from lda.modules.answering.domain import codes
from lda.modules.answering.domain.candidates import CandidateDocuments
from lda.modules.answering.domain.vocabulary import DOCUMENT_KIND, READ_DOCUMENT, AnswerOutcome
from lda.modules.conversations.api import (
    AppendAssistantTurnCommand,
    AppendedMessageView,
    AppendUserMessageCommand,
    ClientMessageId,
    ConversationId,
)
from lda.modules.grounded_generation.api import (
    ClaimReference,
    GenerateGroundedAnswerCommand,
    claims_from_references,
)
from lda.modules.retrieval.api import SearchQuery
from lda.shared.kernel.context import ExecutionContext, TenantContext
from lda.shared.kernel.declarations import not_audited, requires
from lda.shared.kernel.errors import (
    ApplicationError,
    AuthorizationError,
    ConflictError,
    InfrastructureError,
    LdaError,
    ValidationError,
)
from lda.shared.kernel.sensitive import Sensitive


@requires("answering.answer_question", "documents.read_document")
@not_audited(
    reason=(
        "Emits no fact of its own: the user message, the generation record and the assistant "
        "turn are each audited by the use case that writes them"
    )
)
class Answer:
    """Validate -> authorize -> user message -> search -> generate -> link the assistant turn.

    Order is a security property (ADR-040 D2). Nothing is written before the caller's access to at
    least one candidate has been decided; nothing is searched or sent before the user message is
    stored; an answer is returned only after the assistant turn is linked to its generation record.
    Each step is its own transaction in its own module: this use case never claims they are one.
    Failures:
    - before the user message is stored (shape, authorization, conversation errors): nothing is
      written;
    - after it (search, generation): the immutable user message stays and the error is the
      callee's stable code; a retry with the same `client_message_id` and text replays the message;
    - after generation succeeded but the link failed: `answering_turn_not_recorded`, no answer is
      returned, and the generation record stays unlinked (ADR-040 D5).
    No question, quote or answer text is logged, audited or placed in an error.
    """

    def __init__(
        self,
        scopes: ScopeResolver,
        search: ChunkSearch,
        generate: AnswerGenerator,
        user_messages: UserMessageWriter,
        assistant_turns: AssistantTurnWriter,
        limits: AnswerLimits,
    ) -> None:
        self._scopes = scopes
        self._search = search
        self._generate = generate
        self._user_messages = user_messages
        self._assistant_turns = assistant_turns
        self._limits = limits

    def execute(self, ctx: ExecutionContext, command: AnswerCommand) -> AnsweredTurn:
        if not isinstance(ctx, TenantContext):  # system work is never a user's question
            raise AuthorizationError(codes.REQUIRES_PRINCIPAL)
        question, candidates = self._checked(command)
        scope, proof = self._authorize(ctx, candidates)
        granted = frozenset(r.id for r in scope.resources)
        user = self._user_messages.execute(
            ctx,
            AppendUserMessageCommand(command.conversation_id, command.client_message_id, question),
        )
        result = self._search.execute(
            ctx, SearchQuery(scope, proof, question, self._limits.max_sources)
        )
        if any(str(h.document_id.value) not in granted for h in result.hits):
            raise AuthorizationError(codes.NOT_AUTHORIZED)  # a hit is never a grant
        if not result.hits:  # nothing to ground on: no generation, so no record and no turn
            return AnsweredTurn(AnswerOutcome.INSUFFICIENT_EVIDENCE, (), user)
        answer = self._generate.execute(
            ctx,
            GenerateGroundedAnswerCommand(
                Sensitive(question),
                claims_from_references(
                    tuple(
                        ClaimReference(
                            h.document_id.value,
                            h.version_id.value,
                            h.chunk_id.value,
                            h.ordinal,
                            h.text_hash,
                        )
                        for h in result.hits
                    )
                ),
                self._limits.config_id,
            ),
        )
        assistant = None
        if answer.record_id is not None:
            assistant = self._link(ctx, command.conversation_id, answer.record_id)
        return AnsweredTurn(
            AnswerOutcome(answer.outcome.value),
            answer.statements,
            user,
            answer.record_id,
            assistant,
            answer.rejection,
        )

    def _checked(self, command: AnswerCommand) -> tuple[str, CandidateDocuments]:
        if (
            not isinstance(command, AnswerCommand)
            or not isinstance(command.conversation_id, ConversationId)
            or not isinstance(command.client_message_id, ClientMessageId)
        ):
            raise ValidationError(codes.INVALID_COMMAND)
        question = command.question
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > self._limits.max_question_characters
            or "\x00" in question
        ):
            raise ValidationError(codes.INVALID_QUESTION)
        return question, CandidateDocuments.within(
            command.candidate_document_ids, self._limits.max_candidates
        )

    def _authorize(
        self, ctx: TenantContext, candidates: CandidateDocuments
    ) -> tuple[AccessScope, str]:
        """The granted subset of the candidates and the proof token for exactly that subset. A
        denial, an unknown principal, an unregistered action and every inconsistency share one
        code."""
        asked = frozenset(ResourceRef(DOCUMENT_KIND, str(i)) for i in candidates.ids)
        decision = self._scopes.execute(ctx, AuthorizationRequest(ActionId(READ_DOCUMENT), asked))
        scope, proof = decision.scope, decision.proof
        if (
            not decision.allowed
            or scope is None
            or proof is None
            or scope.tenant_id != ctx.tenant_id
            or scope.principal_id != ctx.principal_id
            or not scope.resources
            or not scope.resources <= asked  # never wider than what the caller named
        ):
            raise AuthorizationError(codes.NOT_AUTHORIZED)
        return scope, proof.token

    def _link(
        self, ctx: TenantContext, conversation: ConversationId, record_id: UUID
    ) -> AppendedMessageView:
        try:
            return self._assistant_turns.execute(
                ctx, AppendAssistantTurnCommand(conversation, record_id)
            )
        except LdaError as exc:
            raise _not_recorded(exc) from None


def _not_recorded(cause: LdaError) -> LdaError:
    """Same category as the cause (so a boundary can still tell a conflict from an outage), a
    stable answering code, and only the cause's code as internal detail."""
    if isinstance(cause, ConflictError):
        return ConflictError(codes.TURN_NOT_RECORDED, detail=cause.code)
    if isinstance(cause, InfrastructureError):
        return InfrastructureError(codes.TURN_NOT_RECORDED, detail=cause.code)
    return ApplicationError(codes.TURN_NOT_RECORDED, detail=cause.code)
