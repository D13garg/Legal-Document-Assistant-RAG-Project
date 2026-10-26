from __future__ import annotations

import math
from uuid import UUID

import pytest

from lda.modules.retrieval.domain.embedding import (
    EMBEDDING_DIMENSIONS,
    EmbeddingIdentity,
    checked_vector,
)
from lda.modules.retrieval.domain.ids import ChunkId, DocumentId, VersionId
from lda.modules.retrieval.domain.selection import (
    Candidate,
    VersionFact,
    current_version,
    keep_current,
)
from lda.modules.retrieval.testing import FAKE_IDENTITY, FakeEmbedder
from lda.shared.kernel.errors import ConflictError, ValidationError


def _v(n: int) -> VersionId:
    return VersionId(UUID(int=n))


def test_index_id_is_deterministic_and_changes_with_every_identity_part() -> None:
    base = EmbeddingIdentity("p", "m", "1")
    assert base.index_id == EmbeddingIdentity("p", "m", "1").index_id
    assert len(base.index_id) == 32
    others = {
        EmbeddingIdentity("q", "m", "1").index_id,
        EmbeddingIdentity("p", "n", "1").index_id,
        EmbeddingIdentity("p", "m", "2").index_id,
    }
    assert base.index_id not in others and len(others) == 3


@pytest.mark.parametrize("bad", ["", " x", "a/b", "x" * 65, "é"])
def test_identity_parts_are_validated(bad: str) -> None:
    with pytest.raises(ValidationError):
        EmbeddingIdentity(bad, "m", "1")


def test_dimensions_are_fixed_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        EmbeddingIdentity("p", "m", "1", dimensions=384)


def test_fake_embeddings_are_deterministic_unit_vectors_of_the_fixed_size() -> None:
    first, second = FakeEmbedder(), FakeEmbedder()
    a = first.embed_query("The indemnity clause survives termination")
    assert tuple(a) == tuple(second.embed_query("The indemnity clause survives termination"))
    assert len(a) == EMBEDDING_DIMENSIONS
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, abs_tol=1e-9)
    assert checked_vector(a) == tuple(a)


def test_fake_embedding_ranks_shared_words_higher() -> None:
    e = FakeEmbedder()
    q = e.embed_query("payment due monthly")
    near = e.embed_documents(["payment is due monthly in advance"])[0]
    far = e.embed_documents(["governing law of the state of new york"])[0]
    assert sum(x * y for x, y in zip(q, near, strict=True)) > sum(
        x * y for x, y in zip(q, far, strict=True)
    )


@pytest.mark.parametrize(
    "vector",
    [
        [0.0] * EMBEDDING_DIMENSIONS,  # zero norm
        [1.0] * EMBEDDING_DIMENSIONS,  # not unit length
        [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) + [0.0],  # wrong size
        [math.nan] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
        [math.inf] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
        [True] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
        "x" * EMBEDDING_DIMENSIONS,
    ],
)
def test_malformed_vectors_are_refused(vector: object) -> None:
    with pytest.raises(ValidationError):
        checked_vector(vector)  # type: ignore[arg-type]


def test_current_version_is_the_highest_ready_number() -> None:
    facts = [
        VersionFact(_v(1), 1, True),
        VersionFact(_v(2), 2, True),
        VersionFact(_v(3), 3, False),  # newer but pending/failed: ignored
    ]
    chosen = current_version(facts)
    assert chosen is not None and (chosen.version_id, chosen.number) == (_v(2), 2)


def test_no_ready_version_means_no_current_version() -> None:
    assert current_version([VersionFact(_v(1), 1, False)]) is None
    assert current_version([]) is None


def test_two_ready_versions_with_one_number_are_refused() -> None:
    with pytest.raises(ConflictError):
        current_version([VersionFact(_v(1), 1, True), VersionFact(_v(2), 1, True)])


def test_keep_current_drops_other_versions_and_undecided_documents() -> None:
    d1, d2 = DocumentId(UUID(int=1)), DocumentId(UUID(int=2))

    def cand(d: DocumentId, v: VersionId, n: int) -> Candidate:
        return Candidate(d, v, ChunkId(UUID(int=n)), 0, "a" * 64, 0.5)

    kept = keep_current(
        [cand(d1, _v(1), 10), cand(d1, _v(2), 11), cand(d2, _v(9), 12)],
        {d1: VersionFact(_v(2), 2, True), d2: None},
    )
    assert [c.chunk_id.value.int for c in kept] == [11]


def test_the_fake_identity_is_the_documented_one() -> None:
    assert (FAKE_IDENTITY.provider, FAKE_IDENTITY.model) == ("fake", "hashed-bag-of-words")
