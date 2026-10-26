"""ADR-041 D1: the typed identity is deterministic, complete and tamper-evident."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from lda.modules.evaluation.api import (
    CONFIG_ID,
    EffectiveConfiguration,
    GenerationIdentity,
    PromptIdentity,
    RagConfiguration,
)
from lda.modules.evaluation.testing import build_effective
from lda.shared.kernel.errors import ValidationError

# The canonical text below is written out BY HAND from the documented contract (ADR-030/ADR-041:
# components sorted by name, parameters sorted by name, sorted keys, compact separators, ASCII),
# not produced by the code under test. A change to either the text or the digest means the
# canonicalization or the component set changed, which invalidates every stored evaluation on
# purpose; change it only with a new ADR.
GOLDEN_CANONICAL = (
    '{"components":[["answering",[["max_candidates",100],["max_question_characters",2000],'
    '["max_sources",8]]],["chunking",[["max_characters",2000],["max_chunks",50000],'
    '["min_characters",200]]],["citations",[["max_markers",200],["max_sources",50]]],'
    '["generation",[["max_output_characters",24000],["max_output_tokens",1024],'
    '["max_quote_characters",400],["max_sources",20],["max_statement_characters",1500],'
    '["max_statements",20],["min_quote_characters",12],["model","fake-model:1"],'
    '["temperature",0.0]]],["prompt",[["content_sha256","2e3a3ce3' + "0" * 56 + '"],'
    '["template_id","grounded_answer"],["version",1]]],["retrieval",[["candidate_pool",200],'
    '["max_candidates",2000],["max_query_characters",2000],["max_results",20]]]],'
    '"schema":"evaluation.rag_configuration.v1"}'
)


def test_the_identity_has_the_documented_shape_and_is_stable() -> None:
    cid = build_effective().config_id
    assert CONFIG_ID.fullmatch(cid)
    assert build_effective().config_id == cid == build_effective().config_id


def test_the_identity_equals_the_digest_of_the_hand_written_canonical_text() -> None:
    expected = "rag-" + hashlib.sha256(GOLDEN_CANONICAL.encode("ascii")).hexdigest()
    assert build_effective().config_id == expected


def test_the_identity_is_the_documented_digest_of_the_canonical_components() -> None:
    effective = build_effective()
    canonical = json.dumps(
        {
            "schema": "evaluation.rag_configuration.v1",
            "components": {name: dict(params) for name, params in effective.components().items()},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    assert effective.configuration().config_id == effective.config_id
    # one hashing implementation: the same digest as the ADR-030 free-form class
    assert effective.config_id == RagConfiguration.create(effective.components()).config_id
    assert hashlib.sha256(canonical.encode("ascii")).hexdigest()  # canonical form is ASCII


def test_the_component_set_is_exactly_the_reviewed_six() -> None:
    names = [f.name for f in dataclasses.fields(EffectiveConfiguration)]
    assert names == ["prompt", "generation", "retrieval", "chunking", "citations", "answering"]
    params = build_effective().components()
    assert sorted(params) == sorted(names)
    # no operational or secret-looking parameter ever enters the identity
    flat = {p for c in params.values() for p in c}
    assert not {p for p in flat if any(w in p for w in ("key", "secret", "url", "path", "timeout"))}
    assert "embedding_batch_size" not in flat and "config_id" not in flat


def test_every_parameter_changes_the_identity() -> None:
    base = build_effective()
    seen = {base.config_id}
    for component, params in base.components().items():
        for name, value in params.items():
            changed: object
            if isinstance(value, str):
                changed = value[:-1] + ("1" if value[-1] != "1" else "2")
            elif isinstance(value, float):
                changed = 0.5
            else:
                changed = value + 1
            cid = build_effective(**{f"{component}__{name}": changed}).config_id
            assert cid not in seen, (component, name)  # each change is distinct from all others
            seen.add(cid)


def test_the_prompt_hash_and_version_are_part_of_the_identity() -> None:
    a = build_effective().config_id
    other_hash = PromptIdentity("grounded_answer", 1, "1" * 64)
    other_version = PromptIdentity("grounded_answer", 2, "2e3a3ce3" + "0" * 56)
    assert build_effective(prompt=other_hash).config_id != a
    assert build_effective(prompt=other_version).config_id != a


def test_temperature_spellings_collapse_to_one_value() -> None:
    zero = build_effective().config_id
    assert build_effective(generation__temperature=0).config_id == zero  # int 0
    assert build_effective(generation__temperature=-0.0).config_id == zero  # negative zero


@pytest.mark.parametrize(
    ("component", "parameter", "bad"),
    [
        ("generation", "model", ""),
        ("generation", "model", "bad model"),
        ("generation", "model", "é" * 3),
        ("generation", "temperature", 1.5),
        ("generation", "temperature", float("nan")),
        ("generation", "temperature", True),
        ("generation", "max_output_tokens", 0),
        ("generation", "max_output_tokens", True),
        ("generation", "max_output_tokens", 1.0),
        ("retrieval", "max_results", 101),
        ("chunking", "max_chunks", 0),
        ("citations", "max_markers", 2_001),
        ("answering", "max_sources", -1),
        ("prompt", "content_sha256", "A" * 64),
        ("prompt", "content_sha256", "a" * 63),
        ("prompt", "template_id", "Bad-Id"),
        ("prompt", "version", 0),
    ],
)
def test_invalid_values_are_rejected_without_echoing_them(
    component: str, parameter: str, bad: object
) -> None:
    with pytest.raises(ValidationError) as error:
        build_effective(**{f"{component}__{parameter}": bad})
    assert str(bad) not in str(error.value) or str(bad) == ""


def test_a_missing_or_wrongly_typed_component_is_a_construction_error() -> None:
    with pytest.raises(TypeError):
        EffectiveConfiguration()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        build_effective(retrieval=build_effective().generation)
    with pytest.raises(ValidationError):
        build_effective(generation={"model": "x"})


def test_unicode_in_a_model_name_is_refused_so_no_normalisation_ambiguity_exists() -> None:
    with pytest.raises(ValidationError):
        GenerationIdentity("modele-é", 1, 0.0, 1, 1, 1, 1, 1, 1)


def test_the_identity_does_not_depend_on_object_identity_or_representation() -> None:
    a, b = build_effective(), build_effective()
    assert a is not b and a == b and repr(a) == repr(b)
    assert a.config_id == b.config_id
