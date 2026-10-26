"""Conversations wiring (ADR-039): the only place that joins its use cases to the SQL adapters.

The caller passes the `GenerationRecordRepository` (production: `build_generation_records` from the
citations wiring; tests: the in-memory twin), so this module never touches citations internals.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.bootstrap.container import PlatformContainer
from lda.modules.citations.api import GenerationRecordRepository
from lda.modules.conversations.api import (
    AppendAssistantTurn,
    AppendUserMessage,
    ConversationLimits,
    DeleteConversation,
    ListConversations,
    ReadConversation,
    StartConversation,
)
from lda.modules.conversations.config import ConversationsSettings
from lda.modules.conversations.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.conversations.infrastructure.persistence.unit_of_work import (
    SqlConversationsUnitOfWorkFactory,
)
from lda.platform.configuration.loader import ConfigurationLoader

CONVERSATIONS_MIGRATIONS = MIGRATIONS_VERSIONS
"""Pass as `extra_locations` to `platform.database.migrate.upgrade` (creates the schema)."""


@dataclass(frozen=True)
class ConversationsContainer:
    start_conversation: StartConversation
    append_user_message: AppendUserMessage
    append_assistant_turn: AppendAssistantTurn
    read_conversation: ReadConversation
    list_conversations: ListConversations
    delete_conversation: DeleteConversation


def load_conversations_settings(config: ConfigurationLoader) -> ConversationsSettings:
    return config.load("conversations", ConversationsSettings)


def build_conversations(
    platform: PlatformContainer,
    settings: ConversationsSettings,
    records: GenerationRecordRepository,
) -> ConversationsContainer:
    limits = ConversationLimits(settings.max_messages, settings.max_text_chars, settings.max_list)
    units_of_work = SqlConversationsUnitOfWorkFactory(platform.units_of_work)
    return ConversationsContainer(
        start_conversation=StartConversation(units_of_work, platform.clock, platform.ids),
        append_user_message=AppendUserMessage(units_of_work, platform.clock, limits),
        append_assistant_turn=AppendAssistantTurn(units_of_work, records, platform.clock, limits),
        read_conversation=ReadConversation(units_of_work),
        list_conversations=ListConversations(units_of_work, limits),
        delete_conversation=DeleteConversation(units_of_work, platform.clock),
    )
