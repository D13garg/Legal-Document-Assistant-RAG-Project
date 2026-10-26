"""The approved development embedder (ADR-007 amendment, ADR-045 D3): behavior and contracts.

It is a hashed bag of words. These tests check mechanics (determinism, dimensions, identity,
input handling), never semantic quality.
"""

from __future__ import annotations

import hashlib
import math
import subprocess
import sys
from pathlib import Path

import pytest

from lda.modules.retrieval.api import DevelopmentEmbedder, Embedder
from lda.modules.retrieval.domain.embedding import (
    EMBEDDING_DIMENSIONS,
    checked_vector,
)
from lda.modules.retrieval.testing.doubles import FAKE_IDENTITY
from lda.shared.kernel.errors import ValidationError

ROOT = Path(__file__).resolve().parents[6]
# Measured from the version-1 implementation: a regression pin, not an independent oracle.
GOLDEN = "7e5ac0344be916be46d5a98b676694ca7ba2fb706ae50f2b13b86531662f0685"
TEXT = "The Supplier shall give ninety days written notice before terminating."


def cosine(a: object, b: object) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # type: ignore[call-overload]


def test_it_satisfies_the_embedder_port() -> None:
    embedder: Embedder = DevelopmentEmbedder()
    assert embedder.identity.dimensions == EMBEDDING_DIMENSIONS == 256


def test_identity_is_stable_valid_and_a_different_index_from_the_test_double() -> None:
    identity = DevelopmentEmbedder().identity
    assert (identity.provider, identity.model, identity.version) == (
        "development",
        "hashed-bag-of-words",
        "1",
    )
    assert identity == DevelopmentEmbedder().identity
    assert identity.index_id == DevelopmentEmbedder().identity.index_id
    assert identity.index_id != FAKE_IDENTITY.index_id  # never silently shared with the double
    assert len(identity.index_id) == 32


def test_vectors_have_the_contract_shape_and_are_unit_length() -> None:
    vector = DevelopmentEmbedder().embed_query(TEXT)
    assert len(vector) == EMBEDDING_DIMENSIONS
    assert all(math.isfinite(v) for v in vector)
    assert abs(math.sqrt(sum(v * v for v in vector)) - 1.0) < 1e-9
    assert checked_vector(vector) == tuple(vector)  # retrieval's own validator accepts it


def test_same_text_same_vector_and_documents_match_queries() -> None:
    embedder = DevelopmentEmbedder()
    assert embedder.embed_query(TEXT) == embedder.embed_query(TEXT)
    assert embedder.embed_documents([TEXT, "other words"])[0] == embedder.embed_query(TEXT)
    assert list(embedder.embed_documents([])) == []


def test_a_golden_vector_forces_a_version_bump_if_the_algorithm_changes() -> None:
    vector = DevelopmentEmbedder().embed_query(TEXT)
    digest = hashlib.sha256(",".join(f"{v:.12f}" for v in vector).encode()).hexdigest()
    assert digest == GOLDEN, "algorithm changed: bump DEVELOPMENT_EMBEDDING.version (new index)"


def test_vectors_are_identical_across_processes_and_hash_seeds() -> None:
    code = (
        "import hashlib\n"
        "from lda.modules.retrieval.api import DevelopmentEmbedder\n"
        f"v = DevelopmentEmbedder().embed_query({TEXT!r})\n"
        "print(hashlib.sha256(','.join(f'{x:.12f}' for x in v).encode()).hexdigest())\n"
    )
    seen = set()
    for seed in ("0", "1", "random"):
        done = subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-c", code],
            cwd=ROOT,
            env={  # explicit and minimal: no copy of the caller's environment
                "PYTHONHASHSEED": seed,
                "PYTHONPATH": str(ROOT / "src"),
                "PATH": "/usr/bin:/bin",
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        seen.add(done.stdout.strip())
    assert len(seen) == 1


def test_case_and_compatibility_forms_do_not_change_the_vector() -> None:
    embedder = DevelopmentEmbedder()
    assert embedder.embed_query("Notice PERIOD") == embedder.embed_query("notice period")
    assert embedder.embed_query("\ufb01ne print") == embedder.embed_query("fine print")  # NFKC


def test_shared_words_rank_above_unrelated_text() -> None:
    embedder = DevelopmentEmbedder()
    query = embedder.embed_query("what is the notice period")
    notice, fees = embedder.embed_documents(
        [
            "The Supplier shall give ninety days written notice before terminating.",
            "Fees are payable within thirty days of the invoice date without set off.",
        ]
    )
    assert cosine(query, notice) > cosine(query, fees)


@pytest.mark.parametrize("text", ["---", "!!! ???", "\u2014", "1 2 3"])
def test_text_without_words_still_gets_a_valid_deterministic_unit_vector(text: str) -> None:
    embedder = DevelopmentEmbedder()
    first = embedder.embed_query(text)
    assert first == embedder.embed_query(text)
    checked_vector(first)


def test_different_punctuation_gets_different_directions() -> None:
    embedder = DevelopmentEmbedder()
    assert embedder.embed_query("---") != embedder.embed_query("???")


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "\n\t ", None, 7, b"bytes", ["list"], "x" * 100_001],
)
def test_malformed_empty_and_oversized_input_is_refused_with_a_fixed_code(bad: object) -> None:
    embedder = DevelopmentEmbedder()
    for call in (embedder.embed_query, lambda t: embedder.embed_documents([t])):
        with pytest.raises(ValidationError) as caught:
            call(bad)  # type: ignore[arg-type]
        assert caught.value.code == "embedding_input_invalid"


def test_the_largest_allowed_input_is_accepted() -> None:
    DevelopmentEmbedder().embed_query("word " * 20_000)


@pytest.mark.parametrize("batch", ["a string is not a batch", b"bytes", 5, None])
def test_a_batch_must_be_a_sequence_of_texts(batch: object) -> None:
    with pytest.raises(ValidationError) as caught:
        DevelopmentEmbedder().embed_documents(batch)  # type: ignore[arg-type]
    assert caught.value.code == "embedding_input_invalid"


def test_errors_never_contain_the_input_text() -> None:
    secret = "CONFIDENTIAL-CLAUSE-" + "x" * 100_100
    with pytest.raises(ValidationError) as caught:
        DevelopmentEmbedder().embed_query(secret)
    assert "CONFIDENTIAL" not in repr(caught.value) + caught.value.detail + str(caught.value)


def test_it_keeps_no_state_between_calls() -> None:
    embedder = DevelopmentEmbedder()
    before = vars(embedder).copy()
    embedder.embed_query("confidential text one")
    embedder.embed_documents(["confidential text two"])
    assert vars(embedder) == before == {}
