"""Characterization pin for ADR-041 / Step 18 decision D5 (owner: keep the omissions).

The configuration identity does NOT include the embedder model or a chunker version. The owner
decided (2026-10-18, D5) to keep that as a documented limitation: changing the identity would
change every `config_id` and so invalidate recorded evaluation eligibility. This test pins the
exact parameter set so that ANY change to it, an addition included, fails here and forces that
consequence to be addressed deliberately and under an explicit authorization. It does not claim
the omission is acceptable for production reproducibility; it records that it exists.
"""

from __future__ import annotations

from test_rag_configuration import BASE, runtime_for

EXPECTED = {
    "prompt": {"content_sha256", "template_id", "version"},
    "generation": {
        "max_output_characters",
        "max_output_tokens",
        "max_quote_characters",
        "max_sources",
        "max_statement_characters",
        "max_statements",
        "min_quote_characters",
        "model",
        "temperature",
    },
    "retrieval": {"candidate_pool", "max_candidates", "max_query_characters", "max_results"},
    "chunking": {"max_characters", "max_chunks", "min_characters"},
    "citations": {"max_markers", "max_sources"},
    "answering": {"max_candidates", "max_question_characters", "max_sources"},
}


def test_the_identity_parameter_set_is_exactly_the_documented_one() -> None:
    components = runtime_for(dict(BASE)).effective.components()
    assert {name: set(params) for name, params in components.items()} == EXPECTED


def test_neither_an_embedder_model_nor_a_chunker_version_is_part_of_the_identity() -> None:
    components = runtime_for(dict(BASE)).effective.components()
    names = {f"{c}.{p}" for c, params in components.items() for p in params}
    assert not [n for n in names if "embed" in n.lower()]
    assert "chunking.version" not in names and "chunking.chunker_version" not in names
