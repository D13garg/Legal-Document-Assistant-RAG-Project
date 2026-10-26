"""Grounded generation wiring (ADR-029): joins the use case to its collaborators.

The caller supplies already-built collaborators: Access Control's `AuthorizeAction` and proof
verifier, a `ChunkContentReader` (citations' `ReadChunksContent` over chunking's `ReadChunks`, built
by the caller so chunk-text access stays where chunking's pins allow it), Documents' lifecycle
reader (`ReadVersionState`), the gateway's `GenerateText` and the prompt renderer. This module
adds the production adapters (the Access Control bridge, the PostgreSQL unit of work that commits
the generation record and its audit fact together, and the reader that lets chunk text out of
`ready` versions only: `ReadChunks` also serves `pending` ones, and the caller-supplied reader is
always wrapped here, so a caller cannot skip the lifecycle gate) and refuses inconsistent settings
at start-up rather than after a provider call.

The prompt catalog decides which template can render. The production catalog serves exactly the
approved `grounded_answer` v1 (ADR-029); any other template id or version fails closed
(`grounded_template_unavailable`) before any provider call.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.bootstrap.container import PlatformContainer
from lda.modules.access_control.api import AuthorizeAction
from lda.modules.ai_gateway.api import GenerateText, GenerationParameters
from lda.modules.citations.api import ChunkContentReader, CitationLimits, TextEvidenceBuilder
from lda.modules.citations.config import CitationsSettings
from lda.modules.citations.infrastructure.persistence.unit_of_work import (
    bind_generation_records,
)
from lda.modules.grounded_generation.api import (
    INVALID_INPUT,
    AnswerDrafter,
    ContractLimits,
    EvidenceAssembler,
    GenerateGroundedAnswer,
    GroundingPolicy,
)
from lda.modules.grounded_generation.config import GroundedGenerationSettings
from lda.modules.grounded_generation.infrastructure.document_access import (
    AuthorizeActionDocumentAccess,
)
from lda.modules.grounded_generation.infrastructure.persistence.unit_of_work import (
    SqlGroundedUnitOfWorkFactory,
)
from lda.modules.grounded_generation.infrastructure.ready_version_content import (
    ReadyVersionContent,
    VersionStateReader,
)
from lda.modules.prompts.api import PromptRenderer
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError
from lda.shared.ports.authorization import ProofVerifier


@dataclass(frozen=True)
class GroundedGenerationContainer:
    generate_answer: GenerateGroundedAnswer


def load_grounded_generation_settings(config: ConfigurationLoader) -> GroundedGenerationSettings:
    return config.load("grounded_generation", GroundedGenerationSettings)


def build_grounded_generation(
    settings: GroundedGenerationSettings,
    evidence_settings: CitationsSettings,
    platform: PlatformContainer,
    authorize_action: AuthorizeAction,
    verifier: ProofVerifier,
    content_reader: ChunkContentReader,
    version_state: VersionStateReader,
    generate_text: GenerateText,
    renderer: PromptRenderer,
) -> GroundedGenerationContainer:
    if settings.max_statements > evidence_settings.max_markers:
        # a full answer must stay citable: refuse now, not after a provider call
        raise ConfigurationError(INVALID_INPUT, detail="max_statements")
    max_sources = min(settings.max_sources, evidence_settings.max_sources)
    policy = GroundingPolicy(
        model=settings.model,
        template_id=settings.template_id,
        template_version=settings.template_version,
        parameters=GenerationParameters(settings.max_output_tokens, settings.temperature),
        contract=ContractLimits(
            settings.max_output_characters,
            settings.max_statements,
            settings.max_statement_characters,
            settings.min_quote_characters,
            settings.max_quote_characters,
        ),
        max_sources=max_sources,
    )
    access = AuthorizeActionDocumentAccess(authorize_action)
    builder = TextEvidenceBuilder(
        ReadyVersionContent(version_state, content_reader),  # only `ready` versions release text
        verifier,
        CitationLimits(max_sources, evidence_settings.max_markers),
    )
    return GroundedGenerationContainer(
        GenerateGroundedAnswer(
            EvidenceAssembler(access, builder, max_sources),
            AnswerDrafter(access, renderer, generate_text, policy),
            SqlGroundedUnitOfWorkFactory(platform.units_of_work, bind_generation_records),
            platform.clock,
            platform.ids,
            policy,
        )
    )
