"""Which document versions can reach a generation? Real lifecycle, real `ReadChunks` (ADR-029).

The unit tests use fakes for Documents and for the chunk store. This file closes that gap: the
version state comes from the real `ReadVersionState`, the text from the real `ReadChunks` over the
real chunking stage, and the claims go through the real `GenerateGroundedAnswer`. Every refusal
test asserts what matters for a security property: the provider was never called and nothing was
recorded, not only that an error was raised.

Finalization finding (Step 14 review): before `ReadyVersionContent`, a `pending` version's chunk
text reached the provider, because `ReadChunks` serves `pending` and the builder never asked about
the lifecycle (ADR-015 section 4: `pending` is never searchable).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from itertools import count
from uuid import UUID

import pytest

from lda.modules.ai_gateway.api import EgressPolicy, GenerateText, GenerationParameters
from lda.modules.chunking.api import DocumentId, ReadChunks, ReadChunksCommand, VersionId
from lda.modules.chunking.tests.support import (
    POLICY,
    SYSTEM_A,
    TEXT,
    Rig,
    build_rig,
    ctx,
)
from lda.modules.citations.api import (
    SOURCE_UNAVAILABLE,
    CitationLimits,
    CitedDocumentId,
    CitedVersionId,
    GenerationOutcome,
    ReadChunksContent,
    SourceClaim,
    TextEvidenceBuilder,
)
from lda.modules.citations.testing import FixedClock, FreshIds
from lda.modules.documents.api import (
    AddDocumentVersionCommand,
    ReadVersionState,
    VersionStatus,
)
from lda.modules.documents.api import DocumentId as SourceDocumentId
from lda.modules.grounded_generation.api import (
    AnswerDrafter,
    ContractLimits,
    EvidenceAssembler,
    GenerateGroundedAnswer,
    GenerateGroundedAnswerCommand,
    GroundingPolicy,
)
from lda.modules.grounded_generation.infrastructure.ready_version_content import (
    ReadyVersionContent,
)
from lda.modules.grounded_generation.tests.support import (
    CONFIG_ID,
    AccessRig,
    ScriptedProvider,
    StagedUnitsOfWork,
    reply,
)
from lda.modules.prompts.api import PromptRenderer
from lda.modules.prompts.testing import catalog_with_proposed_grounded_answer, default_limits
from lda.platform.ratelimit.memory import InMemoryRateLimiter
from lda.platform.testing.doubles import FakeClock
from lda.shared.kernel.errors import NotFoundError
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.rate_limiter import RateLimitPolicy

QUOTE = "shall give ninety days written notice"
BODY = f"# Terms\n\nThe Supplier {QUOTE} before terminating this agreement.\n"
ANSWER = reply(("Notice is ninety days.", "S1", QUOTE))


class Wired:
    """The production wiring of `build_grounded_generation`, with Documents and chunking real."""

    def __init__(self, rig: Rig) -> None:
        from datetime import timedelta

        self.rig = rig
        events: list[str] = []
        self.access = AccessRig(events)
        self.provider = ScriptedProvider(events)
        self.units = StagedUnitsOfWork(ctx().tenant_id)
        self.reader = ReadChunks(rig.chunking, rig.objects, ReadVersionState(rig.documents))
        policy = GroundingPolicy(
            "fake-model:1",
            "grounded_answer",
            1,
            GenerationParameters(512, 0.0),
            ContractLimits(),
            10,
        )
        gateway = GenerateText(
            self.access,
            self.provider,
            InMemoryRateLimiter(FakeClock()),
            EgressPolicy(
                provider_timeout=timedelta(seconds=10),
                max_attempts=1,
                backoff=timedelta(milliseconds=1),
                backoff_cap=timedelta(milliseconds=2),
                rate=RateLimitPolicy(capacity=50, refill_per_second=50.0),
                max_segments=20,
                max_segment_characters=20_000,
                max_request_characters=200_000,
                max_instruction_characters=20_000,
                max_output_tokens=4_096,
            ),
            lambda _d: None,
        )
        builder = TextEvidenceBuilder(
            ReadyVersionContent(ReadVersionState(rig.documents), ReadChunksContent(self.reader)),
            self.access,
            CitationLimits(10, 20),
        )
        self.use_case = GenerateGroundedAnswer(
            EvidenceAssembler(self.access, builder, 10),
            AnswerDrafter(
                self.access,
                PromptRenderer(catalog_with_proposed_grounded_answer(), default_limits()),
                gateway,
                policy,
            ),
            self.units,  # type: ignore[arg-type]
            FixedClock(),
            FreshIds(),
            policy,
        )

    def claim(self, document: DocumentId, version: VersionId) -> SourceClaim:
        stored = self.reader.execute(SYSTEM_A, ReadChunksCommand(document, version))
        c = stored.chunks[0]
        return SourceClaim(
            CitedDocumentId(document.value),
            CitedVersionId(version.value),
            c.chunk_id,
            c.ordinal,
            c.text_hash,
        )

    def ask(self, claim: SourceClaim):  # type: ignore[no-untyped-def]
        self.provider.replies.append(ANSWER)
        return self.use_case.execute(
            ctx(),
            GenerateGroundedAnswerCommand(Sensitive("what is the notice?"), (claim,), CONFIG_ID),
        )

    def assert_nothing_left_the_building(self) -> None:
        assert self.provider.requests == []  # no text reached the provider
        assert self.units.committed_records() == [] and self.units.audit == []


@pytest.fixture
def rig() -> Iterator[Rig]:
    yield build_rig()


def chunked(rig: Rig, status: VersionStatus | None) -> tuple[DocumentId, VersionId]:
    """A real upload, real extraction and chunking; `None` leaves the version `pending`."""
    document, version = rig.upload_document(BODY)
    assert rig.stage.run(rig.request(document, version)).__class__.__name__ == "StageSucceeded"
    if status is not None:
        rig.mark(document, version, status)
    return document, version


def test_a_ready_version_is_answered_from_its_real_text(rig: Rig) -> None:
    world = Wired(rig)
    document, version = chunked(rig, VersionStatus.READY)
    answer = world.ask(world.claim(document, version))
    assert answer.outcome is GenerationOutcome.ANSWERED
    assert len(world.provider.requests) == 1 and len(world.units.committed_records()) == 1


def test_a_pending_version_is_refused_before_any_text_reaches_the_provider(rig: Rig) -> None:
    world = Wired(rig)
    document, version = chunked(rig, None)  # chunked, but the version has not become ready
    claim = world.claim(document, version)  # the claim is valid: real id, ordinal and hash
    with pytest.raises(NotFoundError) as caught:
        world.ask(claim)
    assert caught.value.code == SOURCE_UNAVAILABLE
    world.assert_nothing_left_the_building()


@pytest.mark.parametrize("status", [VersionStatus.FAILED, VersionStatus.QUARANTINED])
def test_a_failed_or_quarantined_version_is_refused(rig: Rig, status: VersionStatus) -> None:
    world = Wired(rig)
    document, version = chunked(rig, None)
    claim = world.claim(document, version)  # obtained while pending, then the verdict arrives
    rig.mark(document, version, status)
    with pytest.raises(NotFoundError) as caught:
        world.ask(claim)
    assert caught.value.code == SOURCE_UNAVAILABLE
    world.assert_nothing_left_the_building()


def test_a_tombstoned_document_is_refused(rig: Rig) -> None:
    world = Wired(rig)
    document, version = chunked(rig, VersionStatus.READY)
    claim = world.claim(document, version)
    rig.tombstone(document)
    with pytest.raises(NotFoundError) as caught:
        world.ask(claim)
    assert caught.value.code == SOURCE_UNAVAILABLE
    world.assert_nothing_left_the_building()


def test_every_refusal_is_the_same_error_so_the_state_cannot_be_probed(rig: Rig) -> None:
    world = Wired(rig)
    arrange: dict[str, Callable[[DocumentId, VersionId], None]] = {
        "pending": lambda d, v: None,
        "failed": lambda d, v: rig.mark(d, v, VersionStatus.FAILED),
        "quarantined": lambda d, v: rig.mark(d, v, VersionStatus.QUARANTINED),
        "deleted": lambda d, v: rig.tombstone(d),
    }
    outcomes: set[tuple[str, str]] = set()
    for make_state in arrange.values():
        document, version = chunked(rig, None)
        claim = world.claim(document, version)
        make_state(document, version)
        with pytest.raises(NotFoundError) as caught:
            world.ask(claim)
        outcomes.add((type(caught.value).__name__, caught.value.code))
    assert len(arrange) == 4
    assert outcomes == {("NotFoundError", SOURCE_UNAVAILABLE)}
    world.assert_nothing_left_the_building()


def test_one_unready_source_refuses_the_whole_request_and_sends_nothing(rig: Rig) -> None:
    world = Wired(rig)
    ready_doc, ready_version = chunked(rig, VersionStatus.READY)
    pending_doc, pending_version = chunked(rig, None)
    claims = (world.claim(ready_doc, ready_version), world.claim(pending_doc, pending_version))
    world.provider.replies.append(ANSWER)
    with pytest.raises(NotFoundError):
        world.use_case.execute(
            ctx(), GenerateGroundedAnswerCommand(Sensitive("q?"), claims, CONFIG_ID)
        )
    world.assert_nothing_left_the_building()


def test_characterization_an_older_ready_version_is_still_a_ready_version(rig: Rig) -> None:
    """NOT a guarantee, a recorded limitation (ADR-029): eligibility is `ready`, currency is not
    enforced here. Retrieval's default scope picks the highest `ready` version (ADR-008 section 5);
    generation takes the claims it is given. If the owner decides that generation must also refuse
    superseded versions, this test is the one to change."""
    from lda.modules.documents.api import AddDocumentVersion
    from lda.modules.documents.application.upload_staging import UploadStaging

    world = Wired(rig)
    first_doc, first_version = chunked(rig, VersionStatus.READY)
    numbers = count(50_000)

    class Offset:
        def new_uuid(self) -> UUID:
            return UUID(int=next(numbers))

    add = AddDocumentVersion(
        rig.documents,
        UploadStaging(rig.documents, rig.objects, rig.clock, Offset(), POLICY),
        rig.clock,
        Offset(),
    )
    view = add.execute(
        ctx(),
        AddDocumentVersionCommand(SourceDocumentId(first_doc.value), TEXT, BODY.encode()),
    )
    second = VersionId(view.version_id.value)
    rig.extract.run(rig.request(first_doc, second, stage="extract"))
    assert rig.stage.run(rig.request(first_doc, second)).__class__.__name__ == "StageSucceeded"
    rig.mark(first_doc, second, VersionStatus.READY)
    answer = world.ask(world.claim(first_doc, first_version))  # the superseded version
    assert answer.outcome is GenerationOutcome.ANSWERED
