"""The supported public surface of prompts (ADR-017). `api.lock` pins signatures."""

from __future__ import annotations

import dataclasses

from lda.modules.prompts import api
from lda.modules.prompts.testing import TEST_ONLY_TEMPLATE_ID

EXPECTED_API = {
    "CONTEXT_EMPTY",
    "INVALID_INPUT",
    "LIMIT_EXCEEDED",
    "TEMPLATE_UNKNOWN",
    "PromptCatalog",
    "PromptContextInput",
    "PromptLimits",
    "PromptRenderer",
    "PromptRequest",
    "PromptTemplate",
    "RenderedContextPart",
    "RenderedPrompt",
    "production_catalog",
}


def test_the_public_names_are_exactly_the_approved_set() -> None:
    assert set(api.__all__) == EXPECTED_API
    assert all(hasattr(api, name) for name in api.__all__)


def test_error_codes_are_the_stable_values() -> None:
    assert api.INVALID_INPUT == "prompt_invalid_input"
    assert api.LIMIT_EXCEEDED == "prompt_limit_exceeded"
    assert api.CONTEXT_EMPTY == "prompt_context_empty"
    assert api.TEMPLATE_UNKNOWN == "prompt_template_unknown"


def test_value_types_are_frozen_dataclasses() -> None:
    for name in ("PromptRequest", "PromptContextInput", "RenderedPrompt", "RenderedContextPart"):
        params = getattr(api, name).__dataclass_params__
        assert params.frozen, name


def test_rendered_prompt_carries_identity_instructions_and_ordered_provenance() -> None:
    fields = [f.name for f in dataclasses.fields(api.RenderedPrompt)]
    assert fields == ["template_id", "template_version", "instructions", "context"]
    assert [f.name for f in dataclasses.fields(api.RenderedContextPart)] == ["resource", "text"]


def test_no_digest_or_telemetry_field_exists_on_the_output() -> None:
    names = {f.name for f in dataclasses.fields(api.RenderedPrompt)}
    assert not {n for n in names if "hash" in n or "digest" in n or "sha" in n}


def test_the_public_selection_path_exposes_only_the_approved_template() -> None:
    catalog = api.production_catalog()
    assert catalog.production is True
    ids = [t.template_id for t in api.production_catalog().templates]
    assert ids == ["grounded_answer"]
    assert [t.version for t in catalog.templates] == [1]
    assert TEST_ONLY_TEMPLATE_ID not in ids
