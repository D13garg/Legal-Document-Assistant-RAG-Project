"""Answering configuration and start-up checks (ADR-040): a misconfiguration fails at start-up,
with field names only, before any message is stored or provider is called."""

from __future__ import annotations

from typing import Any

import pytest

from lda.bootstrap.answering import AnsweringContainer, build_answering, load_answering_settings
from lda.bootstrap.chunking import load_chunking_settings
from lda.bootstrap.citations import load_evidence_settings
from lda.bootstrap.conversations import load_conversations_settings
from lda.bootstrap.grounded_generation import load_grounded_generation_settings
from lda.bootstrap.rag_configuration import bind_runtime_configuration, effective_configuration
from lda.bootstrap.retrieval import load_index_settings
from lda.modules.answering.api import LIMITS_INVALID, Answer
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError

BASE = {
    "LDA_DATABASE__URL": "postgresql+psycopg://x:y@localhost/z",
    "LDA_OBJECT_STORAGE__BUCKET": "bucket",
    "LDA_GROUNDED_GENERATION__MODEL": "fake-model:1",
}


def build(**env: str) -> AnsweringContainer:
    config = ConfigurationLoader({**BASE, **env})
    dummy: Any = object()  # validation runs before any collaborator is touched
    answering = load_answering_settings(config)
    index = load_index_settings(config)
    grounded = load_grounded_generation_settings(config)
    evidence = load_evidence_settings(config)
    runtime = bind_runtime_configuration(
        effective_configuration(
            grounded=grounded,
            index=index,
            evidence=evidence,
            chunking=load_chunking_settings(config),
            answering=answering,
        ),
        answering.config_id,
    )
    return build_answering(
        answering,
        dummy,
        dummy,
        dummy,
        dummy,
        dummy,
        index=index,
        grounded=grounded,
        evidence=evidence,
        conversations=load_conversations_settings(config),
        runtime=runtime,
    )


def test_the_defaults_wire_an_answer_use_case() -> None:
    assert isinstance(build().answer, Answer)


def test_the_declared_config_id_is_optional_since_step_17() -> None:
    """ADR-041 D2: the identity is computed; a declaration is only an assertion."""
    assert load_answering_settings(ConfigurationLoader(BASE)).config_id is None


@pytest.mark.parametrize("bad", ["", "rag-xyz", "RAG-" + "a" * 64, "sha256:" + "a" * 64])
def test_a_malformed_config_id_fails_at_load(bad: str) -> None:
    with pytest.raises(ConfigurationError):
        load_answering_settings(ConfigurationLoader({**BASE, "LDA_ANSWERING__CONFIG_ID": bad}))


@pytest.mark.parametrize(
    ("env", "field"),
    [
        ({"LDA_ANSWERING__MAX_SOURCES": "30"}, "max_sources>retrieval.max_results"),
        (
            {"LDA_ANSWERING__MAX_SOURCES": "12", "LDA_GROUNDED_GENERATION__MAX_SOURCES": "10"},
            "max_sources>grounded_generation.max_sources",
        ),
        (
            {"LDA_ANSWERING__MAX_SOURCES": "12", "LDA_CITATIONS__MAX_SOURCES": "10"},
            "max_sources>grounded_generation.max_sources",
        ),
        (
            {"LDA_ANSWERING__MAX_QUESTION_CHARACTERS": "3000"},
            "max_question_characters>retrieval.max_query_characters",
        ),
        (
            {
                "LDA_ANSWERING__MAX_QUESTION_CHARACTERS": "1500",
                "LDA_CONVERSATIONS__MAX_TEXT_CHARS": "1000",
            },
            "max_question_characters>conversations.max_text_chars",
        ),
    ],
)
def test_limits_that_a_collaborator_would_reject_later_fail_at_start_up(
    env: dict[str, str], field: str
) -> None:
    with pytest.raises(ConfigurationError) as caught:
        build(**env)
    assert caught.value.code == LIMITS_INVALID
    assert field in caught.value.detail
    assert "rag-" not in caught.value.detail  # field names only, never values


def test_limits_equal_to_the_collaborators_are_accepted() -> None:
    build(LDA_ANSWERING__MAX_SOURCES="8", LDA_GROUNDED_GENERATION__MAX_SOURCES="8")
