"""Step 16 end to end on REAL PostgreSQL (ADR-040): upload -> ingestion -> real extract/chunk/index
stages -> real Access Control decision and signed proof -> real pgvector search -> real
`GenerateGroundedAnswer` (evidence from the real chunk store, real record table, real audit table)
-> real conversations tables.

Test doubles, and only these: the malware scanner (scripted), the embedder (the deterministic fake)
and the LLM provider (scripted, fake model). No real provider, embedding service or scanner exists
yet, so this proves the mechanics and the failure semantics, not provider behaviour or quality.
Each test uses a fresh random tenant: the conversation and record tables refuse deletes by design.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.bootstrap.access_control import (
    ACCESS_CONTROL_MIGRATIONS,
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.answering import AnsweringContainer, build_answering, load_answering_settings
from lda.bootstrap.audit import AUDIT_MIGRATIONS
from lda.bootstrap.chunking import (
    CHUNKING_MIGRATIONS,
    build_chunk_reader,
    build_chunking,
    load_chunking_settings,
)
from lda.bootstrap.citations import (
    CITATIONS_MIGRATIONS,
    build_generation_records,
    load_evidence_settings,
)
from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.conversations import (
    CONVERSATIONS_MIGRATIONS,
    ConversationsContainer,
    build_conversations,
    load_conversations_settings,
)
from lda.bootstrap.documents import (
    DOCUMENTS_MIGRATIONS,
    DocsContainer,
    build_docs,
    build_original_reader,
    build_version_state_reader,
    load_docs_settings,
    register_doc_resources,
)
from lda.bootstrap.grounded_generation import (
    build_grounded_generation,
    load_grounded_generation_settings,
)
from lda.bootstrap.identity import IDENTITY_MIGRATIONS, build_identity
from lda.bootstrap.ingestion import INGESTION_MIGRATIONS, build_ingestion, load_ingestion_settings
from lda.bootstrap.processing import (
    PROCESSING_MIGRATIONS,
    build_extraction_reader,
    build_processing,
    load_processing_settings,
)
from lda.bootstrap.rag_configuration import bind_runtime_configuration, effective_configuration
from lda.bootstrap.retrieval import (
    RETRIEVAL_MIGRATIONS,
    build_index,
    load_index_settings,
)
from lda.modules.access_control.api import (
    ActionId,
    GrantAccessCommand,
    ResourceRef,
    ResourceRegistry,
)
from lda.modules.ai_gateway.api import EgressPolicy, FailureKind, GenerateText
from lda.modules.answering.api import (
    NOT_AUTHORIZED,
    TURN_NOT_RECORDED,
    AnswerCommand,
    AnswerOutcome,
)
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.citations.api import ReadChunksContent
from lda.modules.conversations.api import (
    ClientMessageId,
    ConversationId,
    ConversationReference,
    MessageRole,
    StartConversationCommand,
)
from lda.modules.documents.api import (
    DOCUMENT_KIND,
    READ_DOCUMENT,
    DocumentId,
    DocumentReference,
    UploadDocumentCommand,
)
from lda.modules.grounded_generation.tests.support import ScriptedProvider, reply
from lda.modules.identity.api import PrincipalKind, RegisterPrincipalCommand
from lda.modules.ingestion.api import PipelineStage
from lda.modules.ingestion.testing import ScriptedScanner
from lda.modules.prompts.api import PromptRenderer, production_catalog
from lda.modules.prompts.testing import default_limits
from lda.modules.retrieval.testing import FakeEmbedder
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.database.jobs.leases import JobLeaseStore
from lda.platform.database.jobs.maintenance import JobMaintenance
from lda.platform.database.jobs.worker import BackoffPolicy, JobWorker
from lda.platform.database.unit_of_work import SqlUnitOfWorkFactory
from lda.platform.events.codec import EventCodec
from lda.platform.events.publisher import TransactionalEventPublisher
from lda.platform.events.registry import HandlerRegistry
from lda.platform.ratelimit.memory import InMemoryRateLimiter
from lda.platform.testing.doubles import FakeClock, InMemoryObjectStore
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.errors import AuthorizationError, InfrastructureError
from lda.shared.kernel.ids import RequestId, TenantId
from lda.shared.ports.rate_limiter import RateLimitPolicy
from platform_support import Database, provisioned_database

KEY = base64.urlsafe_b64encode(b"answering-e2e-test-key-32-bytes!").decode().rstrip("=")
START = datetime(2026, 1, 1, tzinfo=UTC)
ACTION = ActionId(READ_DOCUMENT)
NOTICE = "The Supplier shall give ninety days written notice before terminating this agreement."
FEES = "Fees are payable within thirty days of the invoice date, without set-off."
QUESTION = "SECRET-QUESTION-8813: what is the notice period?"
QUOTE = "shall give ninety days written notice"
STATEMENT = "The notice period is ninety days."
GOOD = reply((STATEMENT, "S1", QUOTE))


class Stack:
    def __init__(self, db: Database) -> None:
        self.tenant = TenantId(uuid4())
        env = {
            "LDA_DATABASE__URL": db.app_url,
            "LDA_OBJECT_STORAGE__BUCKET": "bucket",
            "LDA_JOBS__QUEUES": '{"events": 1, "documents": 1, "ingestion": 1}',
            "LDA_CHUNKING__MAX_CHARACTERS": "300",
            "LDA_CHUNKING__MIN_CHARACTERS": "20",
            "LDA_RETRIEVAL__CANDIDATE_POOL": "5",
            "LDA_RETRIEVAL__MAX_CANDIDATES": "50",
            "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
            "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
            "LDA_GROUNDED_GENERATION__MODEL": "fake-model:1",
        }
        config = ConfigurationLoader(env)
        base = build_platform(config, audit_factory=SqlAuditRecorder)
        self.engine = create_engine(db.app_url)
        self.clock = FakeClock(START)
        handlers, codec = HandlerRegistry(), EventCodec()
        units = SqlUnitOfWorkFactory(
            self.engine,
            self.clock,
            base.ids,
            SqlAuditRecorder,
            lambda uow, jobs: TransactionalEventPublisher(handlers, codec, jobs, uow),
        )
        self.platform: PlatformContainer = dataclasses.replace(
            base,
            clock=self.clock,
            units_of_work=units,
            handlers=handlers,
            event_codec=codec,
            job_leases=JobLeaseStore(self.engine, self.clock),
            job_maintenance=JobMaintenance(self.engine, self.clock),
            object_store=InMemoryObjectStore(),
        )
        self._base = base
        identity = build_identity(self.platform)
        registry = ResourceRegistry()
        register_doc_resources(registry)
        self.access = build_access_control(
            self.platform, identity, load_access_control_settings(config), registry
        )
        reader = build_original_reader(self.platform, self.access)
        processing = build_processing(self.platform, reader, load_processing_settings(config))
        versions = build_version_state_reader(self.platform, self.access)
        chunking = build_chunking(
            self.platform,
            build_extraction_reader(self.platform, versions),
            load_chunking_settings(config),
        )
        read_chunks = build_chunk_reader(self.platform, versions)
        index_settings = load_index_settings(config)
        retrieval = build_index(
            self.platform,
            chunks=read_chunks,
            versions=versions,
            verifier=self.access.proof_verifier,
            embedder=FakeEmbedder(),
            settings=index_settings,
        )
        stages: dict[str, PipelineStage] = {
            "extract": processing.stage,
            "chunk": chunking.stage,
            "index": retrieval.stage,
        }
        ingestion = build_ingestion(
            self.platform,
            load_ingestion_settings(config),
            scanner=ScriptedScanner(),
            stages=[stages[n] for n in ("extract", "chunk", "index")],
        )
        self.docs: DocsContainer = build_docs(
            self.platform, self.access, ingestion, load_docs_settings(config)
        )
        self.alice = identity.register_principal.execute(
            SystemContext(self.tenant, "answering-e2e"),
            RegisterPrincipalCommand(PrincipalKind.USER, "Alice"),
        ).principal_id
        self.worker = JobWorker(
            self.platform.job_leases,
            {
                **self.docs.job_handlers,
                **ingestion.job_handlers,
                **processing.job_handlers,
                **chunking.job_handlers,
                **retrieval.job_handlers,
            },
            self.platform.job_settings,
            self.clock,
            "worker-1",
            BackoffPolicy(1.0, 10.0, jitter=lambda: 0.0),
            self.platform.job_maintenance,
        )
        self.events: list[str] = []
        self.provider = ScriptedProvider(self.events)
        gateway = GenerateText(
            self.access.proof_verifier,
            self.provider,
            InMemoryRateLimiter(self.clock),
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
            lambda _delay: None,
        )
        grounded_settings = load_grounded_generation_settings(config)
        evidence_settings = load_evidence_settings(config)
        generation = build_grounded_generation(
            grounded_settings,
            evidence_settings,
            self.platform,
            self.access.authorize_action,
            self.access.proof_verifier,
            ReadChunksContent(read_chunks),
            versions,
            gateway,
            PromptRenderer(production_catalog(), default_limits()),
        )
        records = build_generation_records(self.platform)
        conversation_settings = load_conversations_settings(config)
        self.conversations: ConversationsContainer = build_conversations(
            self.platform, conversation_settings, records
        )
        self.search = retrieval.search
        answering_settings = load_answering_settings(config)
        self.runtime = bind_runtime_configuration(
            effective_configuration(
                grounded=grounded_settings,
                index=index_settings,
                evidence=evidence_settings,
                chunking=load_chunking_settings(config),
                answering=answering_settings,
            ),
            answering_settings.config_id,
        )
        self.answering: AnsweringContainer = build_answering(
            answering_settings,
            self.access.resolve_access_scope,
            retrieval.search,
            generation.generate_answer,
            self.conversations.append_user_message,
            self.conversations.append_assistant_turn,
            index=index_settings,
            grounded=grounded_settings,
            evidence=evidence_settings,
            conversations=conversation_settings,
            runtime=self.runtime,
        )

    def ctx(self, tenant: TenantId | None = None) -> TenantContext:
        return TenantContext(tenant or self.tenant, self.alice, RequestId(uuid4()))

    def upload(self, body: str) -> UUID:
        view = self.docs.upload_document.execute(
            self.ctx(), UploadDocumentCommand("Agreement", "text/plain", body.encode())
        )
        return view.document_id.value

    def drain(self, rounds: int = 80) -> None:
        for _ in range(rounds):
            if self.worker.run_once() == 0:
                break

    def ready(self, body: str) -> UUID:
        document = self.upload(body)
        self.drain()
        return document

    def grant(self, *documents: UUID) -> None:
        for document in documents:
            self.access.grant_access.execute(
                self.ctx(),
                GrantAccessCommand(self.alice, ResourceRef(DOCUMENT_KIND, str(document)), ACTION),
            )

    def conversation(self) -> ConversationId:
        return self.conversations.start_conversation.execute(
            self.ctx(), StartConversationCommand()
        ).conversation_id

    def command(
        self,
        conversation: ConversationId,
        *documents: UUID,
        client: ClientMessageId | None = None,
        question: str = QUESTION,
    ) -> AnswerCommand:
        return AnswerCommand(
            conversation, client or ClientMessageId(uuid4()), question, tuple(documents)
        )

    def history(self, conversation: ConversationId) -> Any:
        return self.conversations.read_conversation.execute(
            self.ctx(), ConversationReference(conversation)
        )

    def close(self) -> None:
        self._base.dispose()
        self.engine.dispose()


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_answering_e2e",
        "lda_app_answering_e2e",
        [
            IDENTITY_MIGRATIONS,
            ACCESS_CONTROL_MIGRATIONS,
            AUDIT_MIGRATIONS,
            DOCUMENTS_MIGRATIONS,
            INGESTION_MIGRATIONS,
            PROCESSING_MIGRATIONS,
            CHUNKING_MIGRATIONS,
            RETRIEVAL_MIGRATIONS,
            CITATIONS_MIGRATIONS,
            CONVERSATIONS_MIGRATIONS,
        ],
    ) as db:
        yield db


@pytest.fixture
def oracle(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.admin_url)
    yield engine
    engine.dispose()


@pytest.fixture
def stack(database: Database, oracle: Engine) -> Iterator[Stack]:
    s = Stack(database)
    yield s
    s.close()


def count(oracle: Engine, sql: str, **params: object) -> int:
    with oracle.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


def messages(oracle: Engine, stack: Stack, role: str | None = None) -> int:
    if role is None:
        return count(
            oracle,
            "SELECT count(*) FROM conversations.messages WHERE tenant_id = :t",
            t=stack.tenant.value,
        )
    return count(
        oracle,
        "SELECT count(*) FROM conversations.messages WHERE tenant_id = :t AND role = :r",
        t=stack.tenant.value,
        r=role,
    )


def records(oracle: Engine, stack: Stack) -> int:
    return count(
        oracle,
        "SELECT count(*) FROM citations.generation_records WHERE tenant_id = :t",
        t=stack.tenant.value,
    )


def audit_actions(oracle: Engine, stack: Stack) -> list[str]:
    with oracle.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT action FROM audit.records WHERE tenant_id = :t "
                "AND action LIKE ANY (ARRAY['conversations.%', 'grounded_generation.%']) "
                "ORDER BY occurred_at"
            ),
            {"t": stack.tenant.value},
        )
        return [r[0] for r in rows]


# ---- the answered path ------------------------------------------------------------------------


def test_a_question_is_answered_from_real_evidence_and_linked_in_postgres(
    stack: Stack, oracle: Engine
) -> None:
    document = stack.ready(f"{NOTICE}\n\n{FEES}")
    stack.grant(document)
    conversation = stack.conversation()
    stack.provider.replies.append(GOOD)
    ctx = stack.ctx()
    turn = stack.answering.answer.execute(ctx, stack.command(conversation, document))
    assert turn.outcome is AnswerOutcome.ANSWERED
    assert [s.text.reveal() for s in turn.statements] == [STATEMENT]
    assert turn.statements[0].citation.source.document_id.value == document
    assert turn.assistant_message is not None and turn.record_id is not None
    # the provider saw the real, authorized chunk text and the approved production template
    (request,) = stack.provider.requests
    assert "ninety days" in " ".join(c.reveal() for c in request.context)
    # persisted state: one record, two messages, the assistant row is a reference with no text
    assert records(oracle, stack) == 1 and messages(oracle, stack) == 2
    with oracle.connect() as conn:
        row = conn.execute(
            text(
                "SELECT role, text, record_id FROM conversations.messages "
                "WHERE tenant_id = :t AND role = 'assistant'"
            ),
            {"t": stack.tenant.value},
        ).one()
        record = conn.execute(
            text(
                "SELECT config_id, outcome, template_id, template_version "
                "FROM citations.generation_records WHERE record_id = :r"
            ),
            {"r": turn.record_id},
        ).one()
    assert (row.role, row.text, row.record_id) == ("assistant", None, turn.record_id)
    # the stored id is the COMPUTED identity of the running settings (ADR-041 D2), not a label
    assert tuple(record) == (stack.runtime.config_id, "answered", "grounded_answer", "v1")
    history = stack.history(conversation)
    assert [m.role for m in history.messages] == [MessageRole.USER, MessageRole.ASSISTANT]


def test_one_request_id_and_exactly_one_audit_fact_per_write_and_no_content(
    stack: Stack, oracle: Engine
) -> None:
    document = stack.ready(NOTICE)
    stack.grant(document)
    conversation = stack.conversation()
    stack.provider.replies.append(GOOD)
    ctx = stack.ctx()
    stack.answering.answer.execute(ctx, stack.command(conversation, document))
    actions = audit_actions(oracle, stack)
    assert sorted(actions) == [
        "conversations.assistant_turn_added",
        "conversations.conversation_started",
        "conversations.user_message_added",
        "grounded_generation.generation_recorded",
    ]
    with oracle.connect() as conn:
        facts = list(
            conn.execute(
                text(
                    "SELECT action, row_to_json(a)::text AS body FROM audit.records a "
                    "WHERE tenant_id = :t AND request_id = :r"
                ),
                {"t": stack.tenant.value, "r": ctx.request_id.value},
            )
        )
        stored = " ".join(
            conn.execute(
                text(
                    "SELECT row_to_json(r)::text FROM citations.generation_records r "
                    "WHERE tenant_id = :t UNION ALL "
                    "SELECT row_to_json(m)::text FROM conversations.messages m "
                    "WHERE tenant_id = :t AND role = 'assistant'"
                ),
                {"t": stack.tenant.value},
            ).scalars()
        )
    # one request id ties exactly the three writes together; Answer adds no fact of its own
    assert sorted(f.action for f in facts) == [
        "conversations.assistant_turn_added",
        "conversations.user_message_added",
        "grounded_generation.generation_recorded",
    ]
    blob = " ".join(f.body for f in facts)
    for secret in (QUESTION, "SECRET-QUESTION", NOTICE, FEES, QUOTE, STATEMENT, "ninety"):
        assert secret not in blob and secret not in stored


# ---- authorization and tenancy ------------------------------------------------------------------


def test_without_a_grant_nothing_is_written_searched_or_sent(stack: Stack, oracle: Engine) -> None:
    document = stack.ready(NOTICE)  # uploaded and indexed, but never granted
    conversation = stack.conversation()
    stack.provider.replies.append(GOOD)
    with pytest.raises(AuthorizationError) as caught:
        stack.answering.answer.execute(stack.ctx(), stack.command(conversation, document))
    assert caught.value.code == NOT_AUTHORIZED
    assert messages(oracle, stack) == 0 and records(oracle, stack) == 0
    assert stack.provider.requests == []


def test_another_tenant_cannot_answer_over_this_tenants_documents(
    stack: Stack, oracle: Engine, database: Database
) -> None:
    document = stack.ready(NOTICE)
    stack.grant(document)
    conversation = stack.conversation()
    other = TenantId(uuid4())
    foreign = TenantContext(other, stack.alice, RequestId(uuid4()))
    stack.provider.replies.append(GOOD)
    with pytest.raises(AuthorizationError) as caught:
        stack.answering.answer.execute(foreign, stack.command(conversation, document))
    assert caught.value.code == NOT_AUTHORIZED
    assert stack.provider.requests == []
    assert (
        count(
            oracle,
            "SELECT count(*) FROM conversations.messages WHERE tenant_id = :t",
            t=other.value,
        )
        == 0
    )
    assert messages(oracle, stack) == 0


def test_only_the_granted_document_contributes_evidence(stack: Stack, oracle: Engine) -> None:
    granted = stack.ready(NOTICE)
    withheld = stack.ready("Confidential schedule: the penalty rate is twelve percent per annum.")
    stack.grant(granted)
    conversation = stack.conversation()
    stack.provider.replies.append(GOOD)
    turn = stack.answering.answer.execute(
        stack.ctx(), stack.command(conversation, granted, withheld)
    )
    assert turn.outcome is AnswerOutcome.ANSWERED
    (request,) = stack.provider.requests
    sent = " ".join(c.reveal() for c in request.context) + request.instructions.reveal()
    assert "ninety days" in sent and "penalty rate" not in sent and "twelve percent" not in sent


def test_a_deleted_document_is_refused_and_nothing_is_written(stack: Stack, oracle: Engine) -> None:
    """Deleting a document revokes its grants (Documents -> Access Control), so the caller is
    denied before any write, search or provider call."""
    document = stack.ready(NOTICE)
    stack.grant(document)
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(document)))
    conversation = stack.conversation()
    stack.provider.replies.append(GOOD)
    with pytest.raises(AuthorizationError) as caught:
        stack.answering.answer.execute(stack.ctx(), stack.command(conversation, document))
    assert caught.value.code == NOT_AUTHORIZED
    assert stack.provider.requests == []
    assert messages(oracle, stack) == 0 and records(oracle, stack) == 0


def test_an_expired_proof_stops_the_turn_before_generation(stack: Stack, oracle: Engine) -> None:
    document = stack.ready(NOTICE)
    stack.grant(document)
    conversation = stack.conversation()
    real_search = stack.answering.answer._search  # noqa: SLF001 - the seam under test

    class SlowSearch:
        def execute(self, ctx: Any, query: Any) -> Any:
            stack.clock.advance(timedelta(seconds=120))  # the 60 s proof lifetime has passed
            return real_search.execute(ctx, query)

    stack.answering.answer._search = SlowSearch()  # noqa: SLF001
    stack.provider.replies.append(GOOD)
    with pytest.raises(AuthorizationError):
        stack.answering.answer.execute(stack.ctx(), stack.command(conversation, document))
    assert stack.provider.requests == [] and records(oracle, stack) == 0
    assert messages(oracle, stack, "user") == 1  # the immutable user message stays


# ---- insufficient evidence ----------------------------------------------------------------------


def test_a_granted_but_unindexed_document_gives_insufficient_evidence_and_no_record(
    stack: Stack, oracle: Engine
) -> None:
    document = stack.upload(NOTICE)  # never drained: nothing is chunked or indexed yet
    stack.grant(document)
    conversation = stack.conversation()
    stack.provider.replies.append(GOOD)
    turn = stack.answering.answer.execute(stack.ctx(), stack.command(conversation, document))
    assert turn.outcome is AnswerOutcome.INSUFFICIENT_EVIDENCE and turn.statements == ()
    assert turn.record_id is None and turn.assistant_message is None
    assert stack.provider.requests == [] and records(oracle, stack) == 0
    assert messages(oracle, stack) == 1 and messages(oracle, stack, "assistant") == 0


# ---- partial failure and retries on real transactions ---------------------------------------------


def test_a_provider_outage_keeps_the_user_message_and_the_retry_does_not_duplicate_it(
    stack: Stack, oracle: Engine
) -> None:
    document = stack.ready(NOTICE)
    stack.grant(document)
    conversation = stack.conversation()
    client = ClientMessageId(uuid4())
    stack.provider.failures.append(FailureKind.TIMEOUT)
    with pytest.raises(Exception) as caught:
        stack.answering.answer.execute(
            stack.ctx(), stack.command(conversation, document, client=client)
        )
    assert "SECRET-QUESTION" not in str(caught.value)
    assert messages(oracle, stack, "user") == 1 and records(oracle, stack) == 0
    stack.provider.replies.append(GOOD)
    turn = stack.answering.answer.execute(
        stack.ctx(), stack.command(conversation, document, client=client)
    )
    assert turn.user_message.replayed is True and turn.outcome is AnswerOutcome.ANSWERED
    assert messages(oracle, stack, "user") == 1 and messages(oracle, stack, "assistant") == 1


def test_a_failed_link_leaves_an_unlinked_record_and_no_assistant_turn_fact(
    stack: Stack, oracle: Engine
) -> None:
    document = stack.ready(NOTICE)
    stack.grant(document)
    conversation = stack.conversation()
    stack.provider.replies.append(GOOD)
    real = stack.answering.answer._assistant_turns  # noqa: SLF001

    class Down:
        def execute(self, ctx: Any, command: Any) -> Any:
            raise InfrastructureError("conversations_persistence_failed")

    stack.answering.answer._assistant_turns = Down()  # noqa: SLF001
    try:
        with pytest.raises(InfrastructureError) as caught:
            stack.answering.answer.execute(stack.ctx(), stack.command(conversation, document))
    finally:
        stack.answering.answer._assistant_turns = real  # noqa: SLF001
    assert caught.value.code == TURN_NOT_RECORDED
    assert records(oracle, stack) == 1  # the generation is real and recorded ...
    assert messages(oracle, stack, "assistant") == 0  # ... but not linked, and no answer returned
    assert "conversations.assistant_turn_added" not in audit_actions(oracle, stack)
    assert "grounded_generation.generation_recorded" in audit_actions(oracle, stack)


def test_concurrent_identical_requests_store_one_user_message(stack: Stack, oracle: Engine) -> None:
    document = stack.ready(NOTICE)
    stack.grant(document)
    conversation = stack.conversation()
    client = ClientMessageId(uuid4())
    stack.provider.replies.extend([GOOD, GOOD])

    def attempt() -> Any:
        return stack.answering.answer.execute(
            stack.ctx(), stack.command(conversation, document, client=client)
        )

    with ThreadPoolExecutor(2) as pool:
        first, second = (f.result() for f in [pool.submit(attempt), pool.submit(attempt)])
    assert messages(oracle, stack, "user") == 1  # idempotent per client message id
    assert sorted([first.user_message.replayed, second.user_message.replayed]) == [False, True]
    # no exactly-once generation (ADR-040 Q4.4): both calls generated and linked their own record
    assert records(oracle, stack) == 2 and messages(oracle, stack, "assistant") == 2
    assert first.record_id != second.record_id
