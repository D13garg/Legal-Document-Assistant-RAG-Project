"""The answering workflow across the real modules, on in-memory twins (ADR-040).

Real: Access Control (decisions and proofs), Identity, the conversations use cases and their
store, `GenerateGroundedAnswer` with its answer contract, citations evidence building and the record
repository twin. Scripted: the provider and the retrieval search (retrieval's own behaviour has its
suites; the PostgreSQL end-to-end test uses the real index). These tests prove ORDER, PROPAGATION and
FAILURE SEMANTICS of the orchestration, not PostgreSQL behaviour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from lda.modules.access_control.api import (
    ActionId,
    AuthorizationRequest,
    GrantAccessCommand,
    ResourceRef,
)
from lda.modules.access_control.testing import InMemoryAccessControlUnitOfWorkFactory
from lda.modules.access_control.tests.support import CAROL, START, build_rig
from lda.modules.access_control.tests.support import TENANT_B as AC_TENANT_B
from lda.modules.access_control.tests.support import Rig as AccessRig
from lda.modules.ai_gateway.api import FailureKind
from lda.modules.answering.api import (
    NOT_AUTHORIZED,
    TURN_NOT_RECORDED,
    Answer,
    AnswerCommand,
    AnsweredTurn,
    AnswerLimits,
    AnswerOutcome,
)
from lda.modules.conversations.api import (
    AppendAssistantTurn,
    AppendUserMessageCommand,
    ClientMessageId,
    ConversationId,
    ConversationLimits,
    ConversationReference,
    MessageRole,
    StartConversationCommand,
)
from lda.modules.conversations.testing import InMemoryConversationsUnitOfWorkFactory
from lda.modules.conversations.tests.support import Kit, build_kit
from lda.modules.grounded_generation.tests.support import (
    CONFIG_ID,
    PASSAGES,
    PRINCIPAL,
    QUESTION,
    TENANT_A,
    TENANT_B,
    Rig,
    ctx,
    reply,
)
from lda.modules.identity.api import PrincipalReference
from lda.modules.identity.testing import InMemoryIdentityUnitOfWorkFactory, a_principal
from lda.modules.retrieval.api import SearchHit, SearchQuery, SearchResult
from lda.modules.retrieval.domain.ids import ChunkId, DocumentId, VersionId
from lda.platform.testing.doubles import FakeClock
from lda.shared.kernel.context import ExecutionContext, TenantContext
from lda.shared.kernel.errors import (
    ApplicationError,
    ConflictError,
    InfrastructureError,
    LdaError,
    NotFoundError,
)
from lda.shared.kernel.ids import PrincipalId, RequestId

READ = ActionId("documents.read_document")
QUOTE_1 = "shall give ninety days written notice"
QUOTE_2 = "payable within thirty days of the invoice date"
GOOD = reply(
    ("The notice period is ninety days.", "S1", QUOTE_1),
    ("Fees fall due within thirty days.", "S2", QUOTE_2),
)


class Search:
    """Stands in for retrieval: returns the hits of every document inside the granted scope."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.queries: list[SearchQuery] = []
        self.fixed: tuple[SearchHit, ...] | None = None
        self.fail: LdaError | None = None
        self.on_search: Any = None

    def execute(self, c: ExecutionContext, query: SearchQuery) -> SearchResult:
        self.world.events.append("search")
        self.queries.append(query)
        if self.on_search is not None:
            self.on_search()
        if self.fail is not None:
            raise self.fail
        granted = {r.id for r in query.scope.resources}
        hits = self.fixed
        if hits is None:
            hits = tuple(h for h in self.world.hits() if str(h.document_id.value) in granted)
        return SearchResult("index", hits[: query.limit])


class Logged:
    """Logs the call into the shared event list, then delegates (or fails once, on request)."""

    def __init__(self, name: str, inner: Any, events: list[str]) -> None:
        self.name, self.inner, self.events = name, inner, events
        self.fail: LdaError | None = None
        self.calls = 0

    def execute(self, c: ExecutionContext, command: Any) -> Any:
        self.events.append(self.name)
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return self.inner.execute(c, command)


@dataclass
class World:
    access: AccessRig
    gg: Rig
    kit: Kit
    ctx: TenantContext
    events: list[str]
    conversation: ConversationId
    search: Search = field(init=False)
    generate: Logged = field(init=False)
    user: Logged = field(init=False)
    turns: Logged = field(init=False)
    scopes: Logged = field(init=False)
    answer: Answer = field(init=False)

    def hits(self) -> tuple[SearchHit, ...]:
        return tuple(
            SearchHit(
                i + 1,
                DocumentId(d.value),
                VersionId(v.value),
                1,
                ChunkId(c.chunk_id),
                c.ordinal,
                0.5,
                c.text_hash,
            )
            for i, (d, v, c) in enumerate(self.gg.docs)
        )

    def doc_ids(self) -> tuple[UUID, ...]:
        return tuple(d.value for d, _, _ in self.gg.docs)

    def grant(self, *documents: UUID, to: TenantContext | None = None) -> None:
        who = to or self.ctx
        for document in documents:
            self.access.grant.execute(
                who,
                GrantAccessCommand(who.principal_id, ResourceRef("document", str(document)), READ),
            )

    def command(
        self,
        *,
        question: str = QUESTION,
        candidates: tuple[UUID, ...] | None = None,
        client: ClientMessageId | None = None,
        conversation: ConversationId | None = None,
    ) -> AnswerCommand:
        return AnswerCommand(
            conversation or self.conversation,
            client or ClientMessageId(uuid4()),
            question,
            self.doc_ids() if candidates is None else candidates,
        )

    def history(self) -> Any:
        return self.kit.read.execute(self.ctx, ConversationReference(self.conversation))

    def committed_records(self) -> list[Any]:
        return list(self.gg.units.committed_records())


def build_world(*, max_messages: int = 50, max_sources: int = 10) -> World:
    events: list[str] = []
    gg = Rig(events=events)
    clock = FakeClock(START)
    access = build_rig(
        InMemoryIdentityUnitOfWorkFactory(), InMemoryAccessControlUnitOfWorkFactory(), clock
    )
    user_ctx = ctx()  # tenant A, principal 11: the identity the grounded rig also uses
    with access.identity.begin(user_ctx) as uow:
        uow.principals.add(a_principal(tenant_id=TENANT_A, id=PRINCIPAL, now=START))
        uow.commit()
    limits = ConversationLimits(max_messages, 8_000, 50)
    kit = build_kit(InMemoryConversationsUnitOfWorkFactory(), limits)
    conversation = kit.start.execute(user_ctx, StartConversationCommand()).conversation_id
    world = World(access, gg, kit, user_ctx, events, conversation)
    world.search = Search(world)
    world.scopes = Logged("scope", access.resolve, events)
    world.user = Logged("user_message", kit.append_user, events)
    world.generate = Logged("generate", gg.use_case, events)
    world.turns = Logged(
        "link",
        AppendAssistantTurn(kit.factory, gg.units.records, kit.clock, limits),
        events,
    )
    world.answer = Answer(
        world.scopes,
        world.search,
        world.generate,
        world.user,
        world.turns,
        AnswerLimits(CONFIG_ID, 5, 200, max_sources),
    )
    world.grant(*world.doc_ids())
    return world


@pytest.fixture
def world() -> World:
    return build_world()


def run(world: World, **kwargs: Any) -> AnsweredTurn:
    return world.answer.execute(world.ctx, world.command(**kwargs))


def kinds(world: World) -> list[tuple[MessageRole, int]]:
    return [(m.role, m.sequence) for m in world.history().messages]


# ---- the answered path ----------------------------------------------------------------------


def test_a_question_becomes_a_validated_cited_answer_linked_to_its_record(world: World) -> None:
    world.gg.provider.replies.append(GOOD)
    turn = run(world)
    assert turn.outcome is AnswerOutcome.ANSWERED and len(turn.statements) == 2
    assert turn.statements[0].text.reveal() == "The notice period is ninety days."
    (record,) = world.committed_records()
    assert turn.record_id == record.record_id
    assert turn.assistant_message is not None and turn.assistant_message.replayed is False
    assert turn.assistant_message.message.generation_record_id == record.record_id
    assert turn.assistant_message.message.text is None  # the answer is not stored (ADR-039 C1)
    assert turn.user_message.message.text == QUESTION and turn.user_message.replayed is False
    messages = world.history().messages
    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert all(m.text is None for m in messages if m.role is MessageRole.ASSISTANT)


def test_the_steps_run_in_the_documented_order(world: World) -> None:
    world.gg.provider.replies.append(GOOD)
    run(world)
    steps = [
        e for e in world.events if e in {"scope", "user_message", "search", "generate", "link"}
    ]
    assert steps == ["scope", "user_message", "search", "generate", "link"]
    assert world.events.index("provider") > world.events.index("search")
    assert world.events.index("link") > world.events.index("provider")


def test_search_receives_the_granted_scope_proof_text_and_limit(world: World) -> None:
    world.gg.provider.replies.append(GOOD)
    run(world)
    (query,) = world.search.queries
    assert {r.id for r in query.scope.resources} == {str(i) for i in world.doc_ids()}
    assert (query.scope.tenant_id, query.scope.principal_id) == (TENANT_A, PRINCIPAL)
    assert query.text == QUESTION and query.limit == 10 and query.proof_token
    assert QUESTION not in repr(query)


def test_the_generation_gets_evidence_in_rank_order_and_the_configured_id(world: World) -> None:
    world.gg.provider.replies.append(GOOD)
    turn = run(world)
    assert [s.citation.marker for s in turn.statements] == ["S1", "S2"]
    (record,) = world.committed_records()
    assert record.config_id == CONFIG_ID


def test_the_whole_turn_carries_one_request_id_into_every_audit_fact(world: World) -> None:
    world.gg.provider.replies.append(GOOD)
    run(world)
    conversation_facts = list(world.kit.factory.store.audit_facts)  # type: ignore[attr-defined]
    facts = [
        f
        for f in [*conversation_facts, *world.gg.units.audit]
        if f.action != "conversations.conversation_started"  # the fixture's own setup
    ]
    assert sorted(f.action for f in facts) == [
        "conversations.assistant_turn_added",
        "conversations.user_message_added",
        "grounded_generation.generation_recorded",
    ]  # exactly one fact per write, no fact of Answer's own
    assert {f.request_id for f in facts} == {world.ctx.request_id}
    assert {f.actor for f in facts} == {PRINCIPAL}


# ---- authorization, candidates and tenancy ---------------------------------------------------


def test_only_granted_candidates_are_searched_in_a_mixed_set(world: World) -> None:
    first, second, third = world.doc_ids()
    world.access.revoke.execute(
        world.ctx,
        __import__(
            "lda.modules.access_control.api", fromlist=["RevokeAccessCommand"]
        ).RevokeAccessCommand(PRINCIPAL, ResourceRef("document", str(second)), READ),
    )
    stranger = uuid4()
    world.gg.provider.replies.append(reply(("The notice period is ninety days.", "S1", QUOTE_1)))
    turn = run(world, candidates=(first, second, third, stranger))
    (query,) = world.search.queries
    # exactly the granted subset: never widened, never the revoked or the unknown one
    assert {r.id for r in query.scope.resources} == {str(first), str(third)}
    assert turn.outcome is AnswerOutcome.ANSWERED
    sent = " ".join(r.instructions.reveal() for r in world.gg.provider.requests)
    assert PASSAGES[1] not in sent  # the ungranted document's text never reached the provider


def test_no_granted_candidate_writes_nothing_and_searches_nothing(world: World) -> None:
    with pytest.raises(LdaError) as caught:
        run(world, candidates=(uuid4(), uuid4()))  # nobody granted these
    assert caught.value.code == NOT_AUTHORIZED
    assert len(world.history().messages) == 0
    assert world.search.queries == [] and world.generate.calls == 0
    assert world.gg.provider.requests == [] and world.committed_records() == []


def test_another_tenants_principal_is_refused_even_with_valid_document_ids(world: World) -> None:
    foreign = TenantContext(TENANT_B, PRINCIPAL, RequestId(uuid4()))
    with pytest.raises(LdaError) as caught:
        world.answer.execute(foreign, world.command())
    assert caught.value.code == NOT_AUTHORIZED
    assert world.search.queries == [] and world.user.calls == 0
    assert len(world.history().messages) == 0


def test_a_disabled_principal_is_refused_by_access_control_before_any_write(
    world: World,
) -> None:
    world.access.disable.execute(world.ctx, PrincipalReference(PRINCIPAL))
    with pytest.raises(LdaError) as caught:
        run(world)
    assert caught.value.code == NOT_AUTHORIZED
    assert world.user.calls == 0 and world.search.queries == [] and world.generate.calls == 0


class Redirect:
    """Hands `Answer` a REAL decision that Access Control made for a different request (wider
    candidates, another tenant, another principal): no proof is forged, yet the decision does not
    match the caller. A correct `Answer` must refuse it before writing anything."""

    def __init__(self, world: World, build: Any) -> None:
        self.world, self.build = world, build

    def execute(self, c: ExecutionContext, request: Any) -> Any:
        other_ctx, other_request = self.build(c, request)
        return self.world.access.resolve.execute(other_ctx, other_request)


def redirected(world: World, build: Any) -> None:
    world.answer = Answer(
        Redirect(world, build),
        world.search,
        world.generate,
        world.user,
        world.turns,
        AnswerLimits(CONFIG_ID, 5, 200, 10),
    )


def assert_refused_without_effect(world: World) -> None:
    with pytest.raises(LdaError) as caught:
        run(world)
    assert caught.value.code == NOT_AUTHORIZED
    assert world.user.calls == 0 and world.search.queries == [] and world.generate.calls == 0
    assert len(world.history().messages) == 0 and world.gg.provider.requests == []


def test_a_scope_wider_than_the_named_candidates_is_refused(world: World) -> None:
    extra = uuid4()
    world.grant(extra)

    def wider(c: ExecutionContext, request: Any) -> Any:
        more = AuthorizationRequest(
            request.action, request.resources | {ResourceRef("document", str(extra))}
        )
        return c, more

    redirected(world, wider)
    assert_refused_without_effect(world)


def test_a_scope_resolved_for_another_tenant_is_refused(world: World) -> None:
    # CAROL is registered by the access-control rig in ITS tenant B; grant her the same documents
    carol = TenantContext(AC_TENANT_B, CAROL, RequestId(uuid4()))
    world.grant(*world.doc_ids(), to=carol)
    redirected(world, lambda c, request: (carol, request))
    assert_refused_without_effect(world)


def test_a_scope_resolved_for_another_principal_is_refused(world: World) -> None:
    other = TenantContext(TENANT_A, PrincipalId(uuid4()), RequestId(uuid4()))
    with world.access.identity.begin(other) as uow:
        uow.principals.add(a_principal(tenant_id=TENANT_A, id=other.principal_id, now=START))
        uow.commit()
    world.grant(*world.doc_ids(), to=other)
    redirected(world, lambda c, request: (other, request))
    assert_refused_without_effect(world)


def test_a_hit_outside_the_granted_scope_is_never_used(world: World) -> None:
    """A search result is not an authorization grant: a stray hit stops the turn before any
    generation, after the (immutable) user message is stored."""
    stray = SearchHit(
        1, DocumentId(uuid4()), VersionId(uuid4()), 1, ChunkId(uuid4()), 0, 0.9, "b" * 64
    )
    world.search.fixed = (stray,)
    with pytest.raises(LdaError) as caught:
        run(world)
    assert caught.value.code == NOT_AUTHORIZED
    assert world.generate.calls == 0 and world.gg.provider.requests == []
    assert [role for role, _ in kinds(world)] == [MessageRole.USER]


def test_revocation_between_scope_and_generation_stops_the_provider_call(world: World) -> None:
    world.search.on_search = lambda: setattr(world.gg.access, "allowed", set())
    with pytest.raises(LdaError) as caught:
        run(world)
    assert caught.value.code == "grounded_not_authorized"
    assert world.gg.provider.requests == [] and world.committed_records() == []
    assert [role for role, _ in kinds(world)] == [MessageRole.USER]


# ---- conversation preconditions ---------------------------------------------------------------


def test_a_conversation_the_caller_does_not_own_is_not_found_and_nothing_else_runs(
    world: World,
) -> None:
    other = TenantContext(TENANT_A, PrincipalId(uuid4()), RequestId(uuid4()))
    theirs = world.kit.start.execute(other, StartConversationCommand()).conversation_id
    with pytest.raises(NotFoundError) as caught:
        run(world, conversation=theirs)
    assert caught.value.code == "conversation_not_found"
    assert world.search.queries == [] and world.generate.calls == 0


def test_a_full_conversation_is_refused_before_search_or_generation() -> None:
    world = build_world(max_messages=2)
    world.gg.provider.replies.extend([GOOD, GOOD])
    run(world)  # user + assistant fill the two slots
    with pytest.raises(ConflictError) as caught:
        run(world)
    assert caught.value.code == "conversation_limit_reached"
    assert len(world.search.queries) == 1 and world.generate.calls == 1


def test_reusing_a_client_message_id_with_other_text_conflicts_before_search(
    world: World,
) -> None:
    client = ClientMessageId(uuid4())
    world.kit.append_user.execute(
        world.ctx, AppendUserMessageCommand(world.conversation, client, "an earlier question")
    )
    with pytest.raises(ConflictError) as caught:
        run(world, client=client)
    assert caught.value.code == "conversation_message_conflict"
    assert world.search.queries == [] and world.generate.calls == 0
    assert len(world.history().messages) == 1


# ---- insufficient evidence, rejection, refusal ------------------------------------------------


def test_no_evidence_means_no_generation_no_record_and_no_assistant_turn(world: World) -> None:
    world.search.fixed = ()
    turn = run(world)
    assert turn.outcome is AnswerOutcome.INSUFFICIENT_EVIDENCE and turn.statements == ()
    assert turn.record_id is None and turn.assistant_message is None
    assert world.generate.calls == 0 and world.gg.provider.requests == []
    assert world.committed_records() == [] and world.turns.calls == 0
    assert [role for role, _ in kinds(world)] == [MessageRole.USER]


def test_a_rejected_provider_reply_is_never_returned_but_the_turn_is_linked(world: World) -> None:
    world.gg.provider.replies.append("not json at all, with SECRET-PROVIDER-TEXT-7")
    turn = run(world)
    assert turn.outcome is AnswerOutcome.REJECTED and turn.statements == ()
    assert turn.rejection == "grounded_output_malformed"
    assert "SECRET-PROVIDER-TEXT-7" not in repr(turn) + str(turn.rejection)
    assert turn.record_id is not None and turn.assistant_message is not None
    assert [role for role, _ in kinds(world)] == [MessageRole.USER, MessageRole.ASSISTANT]


def test_a_quote_that_is_not_in_the_evidence_is_rejected_not_returned(world: World) -> None:
    world.gg.provider.replies.append(reply(("Invented.", "S1", "a quote that is not in the text")))
    turn = run(world)
    assert turn.outcome is AnswerOutcome.REJECTED and turn.statements == ()
    assert turn.rejection == "grounded_output_quote_unsupported"


def test_a_citation_marker_the_model_invented_is_rejected(world: World) -> None:
    world.gg.provider.replies.append(reply(("Made up source.", "S9", QUOTE_1)))
    turn = run(world)
    assert turn.outcome is AnswerOutcome.REJECTED and turn.statements == ()
    assert turn.rejection == "grounded_output_citation_invalid"


def test_the_models_own_refusal_is_a_linked_insufficient_evidence_turn(world: World) -> None:
    world.gg.provider.replies.append(reply(outcome="insufficient_evidence"))
    turn = run(world)
    assert turn.outcome is AnswerOutcome.INSUFFICIENT_EVIDENCE and turn.statements == ()
    assert turn.record_id is not None and turn.assistant_message is not None


# ---- failures after the user message is stored (ADR-040 Q4.1) ----------------------------------


def test_a_provider_failure_leaves_the_user_message_and_a_retry_replays_it(world: World) -> None:
    world.gg.provider.failures.append(FailureKind.TIMEOUT)
    client = ClientMessageId(uuid4())
    with pytest.raises(Exception) as caught:  # the gateway's stable provider failure
        run(world, client=client)
    assert "SECRET-QUESTION" not in str(caught.value)
    assert [role for role, _ in kinds(world)] == [MessageRole.USER]
    assert world.committed_records() == [] and world.turns.calls == 0
    world.gg.provider.replies.append(GOOD)
    turn = run(world, client=client)
    assert turn.user_message.replayed is True  # the same message, not a duplicate
    assert turn.outcome is AnswerOutcome.ANSWERED
    assert [role for role, _ in kinds(world)] == [MessageRole.USER, MessageRole.ASSISTANT]


def test_a_search_failure_propagates_its_code_and_keeps_the_user_message(world: World) -> None:
    world.search.fail = InfrastructureError("search_unavailable")
    with pytest.raises(InfrastructureError) as caught:
        run(world)
    assert caught.value.code == "search_unavailable"
    assert world.generate.calls == 0
    assert [role for role, _ in kinds(world)] == [MessageRole.USER]


# ---- failure to link after a successful generation (ADR-040 Q4.2) -------------------------------


@pytest.mark.parametrize(
    ("cause", "category"),
    [
        (InfrastructureError("conversations_persistence_failed"), InfrastructureError),
        (ConflictError("conversation_limit_reached"), ConflictError),
        (ConflictError("conversation_record_linked"), ConflictError),
        (NotFoundError("conversation_record_unavailable"), ApplicationError),
        (ApplicationError("conversation_state_changed"), ApplicationError),
    ],
)
def test_a_failed_link_withholds_the_answer_and_reports_it_was_not_recorded(
    world: World, cause: LdaError, category: type[LdaError]
) -> None:
    world.gg.provider.replies.append(GOOD)
    world.turns.fail = cause
    with pytest.raises(LdaError) as caught:
        run(world)
    error = caught.value
    assert error.code == TURN_NOT_RECORDED and type(error) is category
    assert error.detail == cause.code  # internal detail is the cause's code only
    assert "ninety" not in str(error) + error.detail  # no answer text leaks through the error
    assert [role for role, _ in kinds(world)] == [MessageRole.USER]  # no assistant turn
    (orphan,) = world.committed_records()  # the generation really happened and is recorded
    assert orphan.record_id is not None


# ---- retries do not give exactly-once generation (ADR-040 Q4.4) ----------------------------------


def test_a_retry_after_a_failed_link_generates_again_and_orphans_the_first_record(
    world: World,
) -> None:
    world.gg.provider.replies.extend([GOOD, GOOD])
    client = ClientMessageId(uuid4())
    world.turns.fail = InfrastructureError("conversations_persistence_failed")
    with pytest.raises(LdaError):
        run(world, client=client)
    world.turns.fail = None
    turn = run(world, client=client)
    first, second = world.committed_records()
    assert first.record_id != second.record_id  # a second provider call and a second record
    assert len(world.gg.provider.requests) == 2
    assert turn.record_id == second.record_id and turn.user_message.replayed is True
    history = world.history().messages
    assert [m.role for m in history] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert history[1].generation_record_id == second.record_id  # the first stays unlinked


def test_resubmitting_a_completed_turn_is_not_deduplicated(world: World) -> None:
    """Documented limit: nothing here claims exactly-once generation. The same client message id
    replays the user message, but a second answer is generated and linked."""
    world.gg.provider.replies.extend([GOOD, GOOD])
    client = ClientMessageId(uuid4())
    first = run(world, client=client)
    second = run(world, client=client)
    assert second.user_message.replayed is True and first.user_message.replayed is False
    assert first.record_id != second.record_id and len(world.committed_records()) == 2
    roles = [role for role, _ in kinds(world)]
    assert roles == [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.ASSISTANT]


# ---- confidentiality --------------------------------------------------------------------------


def test_no_question_passage_or_answer_text_reaches_the_logs(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    world.gg.provider.replies.extend([GOOD, "garbage SECRET-PROVIDER-TEXT-9"])
    run(world)
    run(world)
    world.turns.fail = InfrastructureError("conversations_persistence_failed")
    world.gg.provider.replies.append(GOOD)
    with pytest.raises(LdaError):
        run(world)
    logged = caplog.text
    for secret in (QUESTION, "SECRET-QUESTION", *PASSAGES, "ninety days", "SECRET-PROVIDER-TEXT"):
        assert secret not in logged


def test_no_audit_fact_carries_content(world: World) -> None:
    world.gg.provider.replies.append(GOOD)
    run(world)
    facts = [*world.kit.factory.store.audit_facts, *world.gg.units.audit]  # type: ignore[attr-defined]
    blob = " ".join(f"{f.action} {f.resource_id} {dict(f.attributes)}" for f in facts)
    for secret in (QUESTION, "SECRET-QUESTION", *PASSAGES, "ninety days", "notice period"):
        assert secret not in blob


@pytest.mark.parametrize("question", ["", "   ", "x" * 201, "has\x00nul"])
def test_an_invalid_question_never_appears_in_the_error(world: World, question: str) -> None:
    probe = question + "SECRET-QUESTION-PROBE" if question.strip() else question
    probe = probe[:200] + "\x00" if "\x00" in question else probe
    with pytest.raises(LdaError) as caught:
        run(world, question="x" * 201 + "SECRET-QUESTION-PROBE" if len(question) > 200 else probe)
    error = caught.value
    assert "SECRET-QUESTION-PROBE" not in str(error) + error.detail + repr(error)
    assert world.user.calls == 0
