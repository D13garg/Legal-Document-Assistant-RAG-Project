"""The lifecycle gate in front of chunk text (ADR-029 section 1 (d)).

`ReadChunks` serves `pending` versions because chunking's own pipeline needs them; generation must
not. Every test asserts the same two things: the outcome, and that the inner text reader was (or
was not) reached, because "refused" is only a security property if no text was read.
"""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from lda.modules.citations.api import (
    SOURCE_UNAVAILABLE,
    BuildEvidenceCommand,
    ChunkContent,
    CitationLimits,
    CitedDocumentId,
    CitedVersionId,
    SourceClaim,
    TextEvidenceBuilder,
    text_sha256,
)
from lda.modules.citations.testing import FakeContent
from lda.modules.documents.api import (
    VersionStatus,
)
from lda.modules.grounded_generation.infrastructure.ready_version_content import (
    PURPOSE,
    ReadyVersionContent,
)
from lda.modules.grounded_generation.tests.support import (
    TENANT_A,
    TENANT_B,
    FakeVersionState,
    ctx,
)
from lda.shared.kernel.errors import InfrastructureError, NotFoundError
from lda.shared.ports.authorization import ProofExpectation

TEXT = "The Supplier shall give ninety days written notice."


class Setup:
    def __init__(self) -> None:
        self.document, self.version = CitedDocumentId(uuid4()), CitedVersionId(uuid4())
        self.chunk = ChunkContent(uuid4(), 0, text_sha256(TEXT), 1, 1, TEXT)
        self.inner = FakeContent()
        self.inner.put(TENANT_A, self.document, self.version, (self.chunk,))
        self.state = FakeVersionState()
        self.reader = ReadyVersionContent(self.state, self.inner)

    def read(self) -> tuple[ChunkContent, ...]:
        return self.reader.read(TENANT_A, self.document, self.version)


def test_a_ready_version_is_read_through_the_inner_reader_under_the_callers_tenant() -> None:
    s = Setup()
    assert s.read() == (s.chunk,)
    ((ctx, query),) = s.state.asked
    assert (ctx.tenant_id, ctx.purpose) == (TENANT_A, PURPOSE)
    assert (query.document_id.value, query.version_id.value) == (s.document.value, s.version.value)
    assert s.inner.reads == 1


@pytest.mark.parametrize(
    "status", [VersionStatus.PENDING, VersionStatus.FAILED, VersionStatus.QUARANTINED]
)
def test_a_version_that_is_not_ready_releases_no_text(status: VersionStatus) -> None:
    s = Setup()
    s.state.status = status
    with pytest.raises(NotFoundError) as caught:
        s.read()
    assert caught.value.code == SOURCE_UNAVAILABLE  # one code: the state cannot be probed
    assert s.inner.reads == 0


@pytest.mark.parametrize("lookalike", ["ready", "READY", 1, True, object()])
def test_only_the_ready_member_itself_is_ready_an_unexpected_value_fails_closed(
    lookalike: object,
) -> None:
    """A status that only looks like `ready` (a string, a number, a future value) is not ready."""
    s = Setup()
    s.state.status = cast(VersionStatus, lookalike)
    with pytest.raises(NotFoundError) as caught:
        s.read()
    assert caught.value.code == SOURCE_UNAVAILABLE
    assert s.inner.reads == 0


def test_an_unknown_or_tombstoned_version_releases_no_text() -> None:
    s = Setup()
    s.state.status = None
    with pytest.raises(NotFoundError):
        s.read()
    assert s.inner.reads == 0


def test_an_answer_about_another_version_is_not_trusted() -> None:
    s = Setup()
    s.state.answer_for = (s.document.value, uuid4())  # READY, but not the version asked about
    with pytest.raises(NotFoundError) as caught:
        s.read()
    assert caught.value.code == SOURCE_UNAVAILABLE
    assert s.inner.reads == 0
    s.state.answer_for = (uuid4(), s.version.value)  # right version id, wrong document
    with pytest.raises(NotFoundError):
        s.read()
    assert s.inner.reads == 0


def test_an_unavailable_lifecycle_service_fails_closed() -> None:
    s = Setup()
    s.state.fail = InfrastructureError("documents_unavailable")
    with pytest.raises(InfrastructureError):
        s.read()
    assert s.inner.reads == 0


def test_another_tenants_version_is_asked_about_under_that_tenant_only() -> None:
    s = Setup()
    s.state.status = None  # Documents answers "not found" across tenants (RLS)
    with pytest.raises(NotFoundError):
        s.reader.read(TENANT_B, s.document, s.version)
    ((ctx, _),) = s.state.asked
    assert ctx.tenant_id == TENANT_B
    assert s.inner.reads == 0


# ---- through the real builder: authorization first, one code out, no cause kept -------------


class Verifier:
    def __init__(self) -> None:
        self.calls = 0

    def verify(self, token: str, expected: ProofExpectation) -> None:
        self.calls += 1


def build(s: Setup, verifier: Verifier) -> None:
    builder = TextEvidenceBuilder(s.reader, verifier, CitationLimits(5, 5))
    claim = SourceClaim(s.document, s.version, s.chunk.chunk_id, 0, s.chunk.text_hash)
    builder.build(ctx(), BuildEvidenceCommand((claim,), "token"))


def test_the_builder_turns_a_pending_version_into_the_single_unavailable_error() -> None:
    s, verifier = Setup(), Verifier()
    s.state.status = VersionStatus.PENDING
    with pytest.raises(NotFoundError) as caught:
        build(s, verifier)
    assert caught.value.code == SOURCE_UNAVAILABLE
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert TEXT not in repr(caught.value) and TEXT not in str(caught.value)
    assert verifier.calls == 1  # the proof was checked first; the second check never happened
    assert s.inner.reads == 0


def test_the_builder_reads_a_ready_version_after_verifying_the_proof() -> None:
    s, verifier = Setup(), Verifier()
    build(s, verifier)
    assert verifier.calls == 2 and s.inner.reads == 1
