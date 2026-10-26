"""Pure rules: parameters, failure kinds, canonical request hash, backoff policy."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from lda.modules.ai_gateway.api import FailureKind, GenerationParameters, ProviderFailure
from lda.modules.ai_gateway.domain.canonical import canonical_request, request_sha256
from lda.modules.ai_gateway.tests.support import policy

BASE: dict[str, Any] = {
    "provider_id": "fake",
    "model": "m1",
    "parameters": GenerationParameters(256, 0.5),
    "instructions": "question",
    "segments": [("document", "d1", "alpha"), ("document", "d2", "beta")],
}


def test_the_hash_is_deterministic_and_matches_the_documented_canonical_form() -> None:
    first, second = request_sha256(**BASE), request_sha256(**BASE)
    assert first == second and len(first) == 64
    document = json.loads(canonical_request(**BASE))
    assert document["v"] == "ai_gateway.request.v1"
    assert list(document) == sorted(document)  # sorted keys
    assert b" " not in canonical_request(**BASE).replace(b"question", b"")  # compact separators


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_id", "other"),
        ("model", "m2"),
        ("parameters", GenerationParameters(256, 0.6)),
        ("parameters", GenerationParameters(255, 0.5)),
        ("instructions", "question!"),
        ("segments", [("document", "d1", "alpha")]),
        ("segments", [("document", "d2", "beta"), ("document", "d1", "alpha")]),  # order matters
        ("segments", [("document", "d1", "alpha"), ("document", "d2", "beta2")]),
        ("segments", [("document", "d1", "alpha"), ("document", "d3", "beta")]),
    ],
)
def test_every_part_of_the_request_changes_the_hash(field: str, value: object) -> None:
    assert request_sha256(**{**BASE, field: value}) != request_sha256(**BASE)


def test_non_ascii_text_has_one_stable_encoding() -> None:
    parts = {**BASE, "instructions": "r\u00e9sum\u00e9 \u2028 \u00e9"}
    assert request_sha256(**parts) == request_sha256(**parts)
    assert canonical_request(**parts).isascii()


@pytest.mark.parametrize(
    "bad",
    [
        {"max_output_tokens": 0},
        {"max_output_tokens": 32_769},
        {"max_output_tokens": True},
        {"max_output_tokens": 1.5},
        {"temperature": -0.1},
        {"temperature": 1.1},
        {"temperature": float("nan")},
        {"temperature": float("inf")},
        {"temperature": "0.5"},
    ],
)
def test_invalid_generation_parameters_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must be"):
        GenerationParameters(**bad)  # type: ignore[arg-type]


def test_an_integer_temperature_is_normalized_to_float() -> None:
    assert GenerationParameters(10, 1).temperature == 1.0
    assert isinstance(GenerationParameters(10, 0).temperature, float)


def test_only_an_unreachable_provider_is_retryable() -> None:
    assert {k for k in FailureKind if k.retryable} == {FailureKind.UNREACHABLE}


def test_a_provider_failure_carries_a_stable_code_and_no_detail() -> None:
    failure = ProviderFailure(FailureKind.TIMEOUT)
    assert failure.code == "provider_timeout" and str(failure) == "provider_timeout"
    assert failure.detail == ""


def test_backoff_is_exponential_deterministic_and_capped() -> None:
    p = policy(backoff=timedelta(milliseconds=100), backoff_cap=timedelta(milliseconds=250))
    assert [p.delay_before_retry(n) for n in (1, 2, 3, 4)] == [
        timedelta(milliseconds=100),
        timedelta(milliseconds=200),
        timedelta(milliseconds=250),
        timedelta(milliseconds=250),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"max_attempts": 0},
        {"max_attempts": 6},
        {"provider_timeout": timedelta(0)},
        {"backoff": timedelta(seconds=5), "backoff_cap": timedelta(seconds=1)},
        {"max_segments": 0},
    ],
)
def test_an_unsafe_policy_cannot_be_built(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must be"):
        policy(**bad)
