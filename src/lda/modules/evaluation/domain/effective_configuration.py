"""`EffectiveConfiguration`: the typed, required identity of a RAG setup (ADR-041 D1).

ADR-030 left components free-form. A release decision cannot rest on free-form strings, so the
identity that release gating accepts is this fixed set of typed components. The composition layer
(`bootstrap/rag_configuration.py`) fills it from the real, validated settings of the modules;
`evaluation` imports none of them (DAG), so each field here mirrors a setting by name and bound.

The identity is computed, never declared: `config_id` is the ADR-030 digest of the canonical form
of these components (`RagConfiguration`, unchanged `evaluation.rag_configuration.v1` rules), so
there is exactly one hashing implementation.

Included (they change evidence selection, the prompt, the model call, output validation or
refusal behaviour): prompt template identity and content hash; generation model and every
generation/validation limit; retrieval result and scan bounds; chunking bounds; citation bounds;
answering limits. Excluded on purpose (ADR-041 D1): credentials, URLs, paths and anything
deployment-specific; provider timeouts, retries and rate limits; request-size ceilings of
`ai_gateway` and `prompts` that only refuse a request and never change a produced answer;
`retrieval.embedding_batch_size` (batching granularity); the evaluation harness's own limits; and
the declared `answering.config_id` itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields

from lda.modules.evaluation.domain.codes import INVALID_INPUT
from lda.modules.evaluation.domain.configuration import RagConfiguration, Scalar
from lda.shared.kernel.errors import ValidationError

_TEMPLATE_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
CONFIG_ID = re.compile(r"rag-[0-9a-f]{64}")


def _int(value: object, field: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValidationError(INVALID_INPUT, detail=field)
    return value


def _text(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValidationError(INVALID_INPUT, detail=field)
    return value


def _unit_float(value: object, field: str) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValidationError(INVALID_INPUT, detail=field)
    number = float(value)  # type: ignore[arg-type]
    if not 0.0 <= number <= 1.0:  # NaN fails this comparison
        raise ValidationError(INVALID_INPUT, detail=field)
    return number + 0.0  # -0.0 becomes 0.0: one spelling per value


@dataclass(frozen=True, slots=True)
class PromptIdentity:
    template_id: str
    version: int
    content_sha256: str  # SHA-256 of the template instructions, UTF-8

    def __post_init__(self) -> None:
        _text(self.template_id, "prompt.template_id", _TEMPLATE_ID)
        _int(self.version, "prompt.version", 1, 1_000_000)
        _text(self.content_sha256, "prompt.content_sha256", _SHA256)


@dataclass(frozen=True, slots=True)
class GenerationIdentity:
    model: str
    max_output_tokens: int
    temperature: float
    max_sources: int
    max_output_characters: int
    max_statements: int
    max_statement_characters: int
    min_quote_characters: int
    max_quote_characters: int

    def __post_init__(self) -> None:
        _text(self.model, "generation.model", _MODEL)
        _int(self.max_output_tokens, "generation.max_output_tokens", 1, 32_768)
        object.__setattr__(
            self, "temperature", _unit_float(self.temperature, "generation.temperature")
        )
        _int(self.max_sources, "generation.max_sources", 1, 500)
        _int(self.max_output_characters, "generation.max_output_characters", 1, 200_000)
        _int(self.max_statements, "generation.max_statements", 1, 200)
        _int(self.max_statement_characters, "generation.max_statement_characters", 1, 20_000)
        _int(self.min_quote_characters, "generation.min_quote_characters", 1, 1_000)
        _int(self.max_quote_characters, "generation.max_quote_characters", 1, 5_000)


@dataclass(frozen=True, slots=True)
class RetrievalIdentity:
    max_results: int
    max_query_characters: int
    candidate_pool: int
    max_candidates: int

    def __post_init__(self) -> None:
        _int(self.max_results, "retrieval.max_results", 1, 100)
        _int(self.max_query_characters, "retrieval.max_query_characters", 1, 20_000)
        _int(self.candidate_pool, "retrieval.candidate_pool", 1, 2_000)
        _int(self.max_candidates, "retrieval.max_candidates", 1, 20_000)


@dataclass(frozen=True, slots=True)
class ChunkingIdentity:
    max_characters: int
    min_characters: int
    max_chunks: int

    def __post_init__(self) -> None:
        _int(self.max_characters, "chunking.max_characters", 1, 10_000_000)
        _int(self.min_characters, "chunking.min_characters", 1, 10_000_000)
        _int(self.max_chunks, "chunking.max_chunks", 1, 10_000_000)


@dataclass(frozen=True, slots=True)
class CitationsIdentity:
    max_sources: int
    max_markers: int

    def __post_init__(self) -> None:
        _int(self.max_sources, "citations.max_sources", 1, 500)
        _int(self.max_markers, "citations.max_markers", 1, 2_000)


@dataclass(frozen=True, slots=True)
class AnsweringIdentity:
    max_candidates: int
    max_question_characters: int
    max_sources: int

    def __post_init__(self) -> None:
        _int(self.max_candidates, "answering.max_candidates", 1, 10_000)
        _int(self.max_question_characters, "answering.max_question_characters", 1, 20_000)
        _int(self.max_sources, "answering.max_sources", 1, 100)


@dataclass(frozen=True, slots=True)
class EffectiveConfiguration:
    """Every component is required: a missing one is a construction error, not a default."""

    prompt: PromptIdentity
    generation: GenerationIdentity
    retrieval: RetrievalIdentity
    chunking: ChunkingIdentity
    citations: CitationsIdentity
    answering: AnsweringIdentity

    def __post_init__(self) -> None:
        expected: dict[str, type] = {
            "prompt": PromptIdentity,
            "generation": GenerationIdentity,
            "retrieval": RetrievalIdentity,
            "chunking": ChunkingIdentity,
            "citations": CitationsIdentity,
            "answering": AnsweringIdentity,
        }
        for name, kind in expected.items():
            if type(getattr(self, name)) is not kind:
                raise ValidationError(INVALID_INPUT, detail=name)

    def components(self) -> dict[str, dict[str, Scalar]]:
        """Component name -> parameter name -> scalar. Built from the declared fields, so a field
        added to a component enters the identity unless this module is changed on purpose."""
        return {
            f.name: {
                p.name: getattr(getattr(self, f.name), p.name)
                for p in fields(getattr(self, f.name))
            }
            for f in fields(self)
        }

    def configuration(self) -> RagConfiguration:
        return RagConfiguration.create(self.components())

    @property
    def config_id(self) -> str:
        return self.configuration().config_id
