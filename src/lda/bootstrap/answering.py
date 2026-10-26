"""Answering wiring (ADR-040): joins `Answer` to the public use cases it calls.

Nothing here makes a decision. The caller passes the already-built use cases (Access Control's
`ResolveAccessScope`, retrieval's `SearchIndex`, `GenerateGroundedAnswer`, the conversations
`AppendUserMessage` and `AppendAssistantTurn`); this module only checks, once at start-up, that the
limits of `answering` fit inside the limits of those collaborators, so a misconfiguration fails
now instead of after a message was stored or a provider was called.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.bootstrap.rag_configuration import (
    RUNTIME_CONFIGURATION_MISMATCH,
    RuntimeConfiguration,
    answering_identity,
    evidence_identity,
    generation_identity,
    search_identity,
)
from lda.modules.access_control.api import ResolveAccessScope
from lda.modules.answering.api import LIMITS_INVALID, Answer, AnswerLimits
from lda.modules.answering.config import AnsweringSettings
from lda.modules.citations.config import CitationsSettings
from lda.modules.conversations.api import AppendAssistantTurn, AppendUserMessage
from lda.modules.conversations.config import ConversationsSettings
from lda.modules.grounded_generation.api import GenerateGroundedAnswer
from lda.modules.grounded_generation.config import GroundedGenerationSettings
from lda.modules.retrieval.api import SearchIndex
from lda.modules.retrieval.config import RetrievalSettings
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError


@dataclass(frozen=True)
class AnsweringContainer:
    answer: Answer


def load_answering_settings(config: ConfigurationLoader) -> AnsweringSettings:
    return config.load("answering", AnsweringSettings)


def build_answering(
    settings: AnsweringSettings,
    scopes: ResolveAccessScope,
    search: SearchIndex,
    generate: GenerateGroundedAnswer,
    user_messages: AppendUserMessage,
    assistant_turns: AppendAssistantTurn,
    *,
    index: RetrievalSettings,
    grounded: GroundedGenerationSettings,
    evidence: CitationsSettings,
    conversations: ConversationsSettings,
    runtime: RuntimeConfiguration,
) -> AnsweringContainer:
    """Refuse limits that a collaborator would reject later (field names only, never values).

    `runtime` carries the computed configuration id (ADR-041 D2). It is used only if it was
    computed from the very settings passed here; otherwise the id would label records with a
    configuration that is not the one running, so start-up fails."""
    problems = [
        name
        for name, fits in (
            ("max_sources>retrieval.max_results", settings.max_sources <= index.max_results),
            (
                "max_sources>grounded_generation.max_sources",
                settings.max_sources <= min(grounded.max_sources, evidence.max_sources),
            ),
            (
                "max_question_characters>retrieval.max_query_characters",
                settings.max_question_characters <= index.max_query_characters,
            ),
            (
                "max_question_characters>conversations.max_text_chars",
                settings.max_question_characters <= conversations.max_text_chars,
            ),
        )
        if not fits
    ]
    if problems:
        raise ConfigurationError(LIMITS_INVALID, detail=", ".join(problems))
    mismatched = [
        name
        for name, same in (
            ("answering", runtime.effective.answering == answering_identity(settings)),
            ("generation", runtime.effective.generation == generation_identity(grounded)),
            ("retrieval", runtime.effective.retrieval == search_identity(index)),
            ("citations", runtime.effective.citations == evidence_identity(evidence)),
        )
        if not same
    ]
    if mismatched:
        raise ConfigurationError(RUNTIME_CONFIGURATION_MISMATCH, detail=", ".join(mismatched))
    limits = AnswerLimits(
        runtime.config_id,
        settings.max_candidates,
        settings.max_question_characters,
        settings.max_sources,
    )
    return AnsweringContainer(
        Answer(scopes, search, generate, user_messages, assistant_turns, limits)
    )
