"""Computes the effective RAG configuration identity from the real settings (ADR-041 D1, D2).

This is the only place that maps module settings to `evaluation`'s typed identity; the hashing
itself lives in `evaluation` (one implementation). The identity is computed, never declared:
`LDA_ANSWERING__CONFIG_ID` is an optional assertion that must equal the computed value, and a
mismatch stops start-up. A well-formed `rag-<64 hex>` string proves nothing.

Errors name fields and codes only, never values.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from lda.modules.answering.config import AnsweringSettings
from lda.modules.chunking.config import ChunkingSettings
from lda.modules.citations.config import CitationsSettings
from lda.modules.evaluation.api import (
    AnsweringIdentity,
    ChunkingIdentity,
    CitationsIdentity,
    EffectiveConfiguration,
    GenerationIdentity,
    PromptIdentity,
    RetrievalIdentity,
)
from lda.modules.grounded_generation.config import GroundedGenerationSettings
from lda.modules.prompts.api import PromptCatalog, production_catalog
from lda.modules.retrieval.config import RetrievalSettings
from lda.shared.kernel.errors import ApplicationError, ConfigurationError

CONFIG_ID_MISMATCH = "rag_config_id_mismatch"
CONFIGURATION_INVALID = "rag_configuration_invalid"
RUNTIME_CONFIGURATION_MISMATCH = "rag_runtime_configuration_mismatch"


def template_identity(
    grounded: GroundedGenerationSettings, catalog: PromptCatalog
) -> PromptIdentity:
    try:
        template = catalog.get(grounded.template_id, grounded.template_version)
    except ApplicationError:
        raise ConfigurationError(CONFIGURATION_INVALID, detail="prompt.template") from None
    digest = hashlib.sha256(template.instructions.encode("utf-8")).hexdigest()
    return PromptIdentity(template.template_id, template.version, digest)


def generation_identity(grounded: GroundedGenerationSettings) -> GenerationIdentity:
    return GenerationIdentity(
        grounded.model,
        grounded.max_output_tokens,
        grounded.temperature,
        grounded.max_sources,
        grounded.max_output_characters,
        grounded.max_statements,
        grounded.max_statement_characters,
        grounded.min_quote_characters,
        grounded.max_quote_characters,
    )


def search_identity(index: RetrievalSettings) -> RetrievalIdentity:
    return RetrievalIdentity(
        index.max_results, index.max_query_characters, index.candidate_pool, index.max_candidates
    )


def chunking_identity(chunking: ChunkingSettings) -> ChunkingIdentity:
    return ChunkingIdentity(chunking.max_characters, chunking.min_characters, chunking.max_chunks)


def evidence_identity(evidence: CitationsSettings) -> CitationsIdentity:
    return CitationsIdentity(evidence.max_sources, evidence.max_markers)


def answering_identity(settings: AnsweringSettings) -> AnsweringIdentity:
    return AnsweringIdentity(
        settings.max_candidates, settings.max_question_characters, settings.max_sources
    )


def effective_configuration(
    *,
    grounded: GroundedGenerationSettings,
    index: RetrievalSettings,
    evidence: CitationsSettings,
    chunking: ChunkingSettings,
    answering: AnsweringSettings,
    catalog: PromptCatalog | None = None,
) -> EffectiveConfiguration:
    """A bound outside a typed identity (for example a chunking limit above its ceiling) is a
    start-up error, not a silently different identity."""
    try:
        return EffectiveConfiguration(
            template_identity(grounded, catalog or production_catalog()),
            generation_identity(grounded),
            search_identity(index),
            chunking_identity(chunking),
            evidence_identity(evidence),
            answering_identity(answering),
        )
    except ApplicationError as error:
        detail = getattr(error, "detail", None) or "configuration"
        raise ConfigurationError(CONFIGURATION_INVALID, detail=str(detail)) from None


@dataclass(frozen=True)
class RuntimeConfiguration:
    """The effective configuration and its computed id. The id cannot be set independently: a
    value that is not the digest of `effective` is refused at construction."""

    effective: EffectiveConfiguration
    config_id: str

    def __post_init__(self) -> None:
        if self.config_id != self.effective.config_id:
            raise ConfigurationError(CONFIG_ID_MISMATCH)


def bind_runtime_configuration(
    effective: EffectiveConfiguration, declared_config_id: str | None
) -> RuntimeConfiguration:
    """`declared_config_id` (from `LDA_ANSWERING__CONFIG_ID`) is optional. When present it must
    equal the computed id; the declaration never overrides the computation."""
    computed = effective.config_id
    if declared_config_id is not None and declared_config_id != computed:
        raise ConfigurationError(CONFIG_ID_MISMATCH)
    return RuntimeConfiguration(effective, computed)
