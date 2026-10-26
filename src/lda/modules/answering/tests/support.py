"""Scripted collaborators for the module-local tests: they record every call so a test can prove
what did and did not happen. Proof-carrying allowed paths are tested in tests/platform with the
real Access Control (only Access Control may mint a proof)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from lda.modules.access_control.api import (
    ActionId,
    AuthorizationDecision,
    AuthorizationRequest,
    DenyReason,
)
from lda.modules.answering.api import Answer, AnswerCommand, AnswerLimits
from lda.modules.conversations.api import (
    AppendAssistantTurnCommand,
    AppendedMessageView,
    AppendUserMessageCommand,
    ClientMessageId,
    ConversationId,
    MessageRole,
    MessageView,
)
from lda.modules.grounded_generation.api import GenerateGroundedAnswerCommand
from lda.modules.retrieval.api import SearchQuery
from lda.shared.kernel.context import ExecutionContext, TenantContext
from lda.shared.kernel.errors import LdaError
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId

CONFIG_ID = "rag-" + "a" * 64
LIMITS = AnswerLimits(CONFIG_ID, max_candidates=3, max_question_characters=60, max_sources=4)
QUESTION = "SECRET-QUESTION-9917: what is the notice period?"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
TENANT = TenantId(UUID(int=0xA1))
PRINCIPAL = PrincipalId(UUID(int=0xB1))


def ctx() -> TenantContext:
    return TenantContext(TENANT, PRINCIPAL, RequestId(uuid4()))


def command(
    *,
    question: str = QUESTION,
    candidates: tuple[UUID, ...] | None = None,
    conversation: ConversationId | None = None,
    client_message: ClientMessageId | None = None,
) -> AnswerCommand:
    return AnswerCommand(
        conversation or ConversationId(uuid4()),
        client_message or ClientMessageId(uuid4()),
        question,
        (uuid4(),) if candidates is None else candidates,
    )


def user_view(conversation: ConversationId, client: ClientMessageId) -> AppendedMessageView:
    return AppendedMessageView(
        MessageView(conversation, 1, MessageRole.USER, NOW, client, QUESTION, None), False
    )


def assistant_view(conversation: ConversationId, record: UUID) -> AppendedMessageView:
    return AppendedMessageView(
        MessageView(conversation, 2, MessageRole.ASSISTANT, NOW, None, None, record), False
    )


@dataclass
class Scopes:
    """Always denies (the only decision a test may build without a proof) or fails."""

    reason: DenyReason = DenyReason.NOT_GRANTED
    error: LdaError | None = None
    requests: list[AuthorizationRequest] = field(default_factory=list)

    def execute(
        self, ctx: ExecutionContext, request: AuthorizationRequest
    ) -> AuthorizationDecision:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return AuthorizationDecision.deny(ActionId("documents.read_document"), self.reason)


@dataclass
class Recorder:
    """What the collaborators that must NOT be reached were asked to do."""

    user_commands: list[AppendUserMessageCommand] = field(default_factory=list)
    queries: list[SearchQuery] = field(default_factory=list)
    generations: list[GenerateGroundedAnswerCommand] = field(default_factory=list)
    turns: list[AppendAssistantTurnCommand] = field(default_factory=list)

    def untouched(self) -> bool:
        return not (self.user_commands or self.queries or self.generations or self.turns)


class UserWriter:
    def __init__(self, recorder: Recorder) -> None:
        self._r = recorder

    def execute(
        self, ctx: ExecutionContext, command: AppendUserMessageCommand
    ) -> AppendedMessageView:
        self._r.user_commands.append(command)
        return user_view(command.conversation_id, command.client_message_id)


class Search:
    def __init__(self, recorder: Recorder) -> None:
        self._r = recorder

    def execute(self, ctx: ExecutionContext, query: SearchQuery) -> object:
        self._r.queries.append(query)
        raise AssertionError("search must not be reached")


class Generator:
    def __init__(self, recorder: Recorder) -> None:
        self._r = recorder

    def execute(self, ctx: ExecutionContext, command: GenerateGroundedAnswerCommand) -> object:
        self._r.generations.append(command)
        raise AssertionError("generation must not be reached")


class Turns:
    def __init__(self, recorder: Recorder) -> None:
        self._r = recorder

    def execute(self, ctx: ExecutionContext, command: AppendAssistantTurnCommand) -> object:
        self._r.turns.append(command)
        raise AssertionError("no turn may be appended")


def build(scopes: Scopes | None = None) -> tuple[Answer, Scopes, Recorder]:
    scopes = scopes or Scopes()
    recorder = Recorder()
    use_case = Answer(
        scopes,
        Search(recorder),  # type: ignore[arg-type]
        Generator(recorder),  # type: ignore[arg-type]
        UserWriter(recorder),
        Turns(recorder),  # type: ignore[arg-type]
        LIMITS,
    )
    return use_case, scopes, recorder
