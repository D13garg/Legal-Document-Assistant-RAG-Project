"""What `answering` needs from the modules it calls, one single-method port each.

The public use cases of `access_control`, `retrieval`, `grounded_generation` and `conversations`
satisfy these structurally, so bootstrap passes them directly and tests can script each one.
"""

from __future__ import annotations

from typing import Protocol

from lda.modules.access_control.api import AuthorizationDecision, AuthorizationRequest
from lda.modules.conversations.api import (
    AppendAssistantTurnCommand,
    AppendedMessageView,
    AppendUserMessageCommand,
)
from lda.modules.grounded_generation.api import GenerateGroundedAnswerCommand, GroundedAnswer
from lda.modules.retrieval.api import SearchQuery, SearchResult
from lda.shared.kernel.context import ExecutionContext


class ScopeResolver(Protocol):
    def execute(
        self, ctx: ExecutionContext, request: AuthorizationRequest
    ) -> AuthorizationDecision: ...


class ChunkSearch(Protocol):
    def execute(self, ctx: ExecutionContext, query: SearchQuery) -> SearchResult: ...


class AnswerGenerator(Protocol):
    def execute(
        self, ctx: ExecutionContext, command: GenerateGroundedAnswerCommand
    ) -> GroundedAnswer: ...


class UserMessageWriter(Protocol):
    def execute(
        self, ctx: ExecutionContext, command: AppendUserMessageCommand
    ) -> AppendedMessageView: ...


class AssistantTurnWriter(Protocol):
    def execute(
        self, ctx: ExecutionContext, command: AppendAssistantTurnCommand
    ) -> AppendedMessageView: ...
