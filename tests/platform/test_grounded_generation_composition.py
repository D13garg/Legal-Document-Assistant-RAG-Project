"""Grounded generation through the real composition: Access Control, the proof verifier, the
PostgreSQL unit of work, the audit table and the generation-record table (ADR-029).

Only the chunk store and the provider are test doubles (chunking has its own end-to-end suite and
there is no real provider adapter yet); everything that decides access or persists is real.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.bootstrap.access_control import (
    ACCESS_CONTROL_MIGRATIONS,
    AccessControlContainer,
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.audit import AUDIT_MIGRATIONS
from lda.bootstrap.citations import CITATIONS_MIGRATIONS
from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.grounded_generation import build_grounded_generation
from lda.bootstrap.identity import IDENTITY_MIGRATIONS, IdentityContainer, build_identity
from lda.modules.access_control.api import (
    ActionId,
    GrantAccessCommand,
    ResourceKindDefinition,
    ResourceRef,
    ResourceRegistry,
    RevokeAccessCommand,
)
from lda.modules.ai_gateway.api import EgressPolicy, GenerateText, GenerationParameters
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.citations.api import (
    ChunkContent,
    ChunkContentReader,
    CitationLimits,
    CitedDocumentId,
    CitedVersionId,
    GenerationOutcome,
    SourceClaim,
    TextEvidenceBuilder,
    text_sha256,
)
from lda.modules.citations.config import CitationsSettings
from lda.modules.citations.infrastructure.persistence.unit_of_work import (
    bind_generation_records,
)
from lda.modules.documents.api import DOCUMENT_KIND, READ_DOCUMENT, VersionStatus
from lda.modules.grounded_generation.api import (
    NOT_AUTHORIZED,
    AnswerDrafter,
    ContractLimits,
    EvidenceAssembler,
    GenerateGroundedAnswer,
    GenerateGroundedAnswerCommand,
    GroundingPolicy,
)
from lda.modules.grounded_generation.config import GroundedGenerationSettings
from lda.modules.grounded_generation.infrastructure.document_access import (
    AuthorizeActionDocumentAccess,
)
from lda.modules.grounded_generation.infrastructure.persistence.unit_of_work import (
    SqlGroundedUnitOfWorkFactory,
)
from lda.modules.grounded_generation.infrastructure.ready_version_content import (
    VersionStateReader,
)
from lda.modules.grounded_generation.tests.support import (
    CONFIG_ID,
    FakeVersionState,
    LoggingContent,
    ScriptedProvider,
    reply,
)
from lda.modules.identity.api import PrincipalKind, RegisterPrincipalCommand
from lda.modules.prompts.api import PromptCatalog, PromptRenderer, production_catalog
from lda.modules.prompts.testing import catalog_with_proposed_grounded_answer, default_limits
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.ratelimit.memory import InMemoryRateLimiter
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.errors import AuthorizationError, ConfigurationError, InfrastructureError
from lda.shared.kernel.ids import RequestId, TenantId
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.rate_limiter import RateLimitPolicy
from platform_support import Database, provisioned_database

KEY = base64.urlsafe_b64encode(b"bootstrap-test-signing-key-32-bytes!!").decode().rstrip("=")
T1, T2 = TenantId(UUID(int=1)), TenantId(UUID(int=2))
TEXTS = (
    "The Supplier shall give ninety days written notice before terminating this agreement.",
    "Fees are payable within thirty days of the invoice date, without set-off.",
)
QUESTION = "SECRET-QUESTION-5521: what is the notice period?"
ANSWER = reply(
    ("The notice period is ninety days.", "S1", "shall give ninety days written notice"),
    ("Fees fall due within thirty days.", "S2", "payable within thirty days of the invoice"),
)


def environment(db: Database) -> dict[str, str]:
    return {
        "LDA_DATABASE__URL": db.app_url,
        "LDA_OBJECT_STORAGE__BUCKET": "bucket",
        "LDA_JOBS__QUEUES": '{"events": 1}',
        "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
        "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
    }


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_grounded_boot",
        "lda_app_grounded_boot",
        [IDENTITY_MIGRATIONS, ACCESS_CONTROL_MIGRATIONS, AUDIT_MIGRATIONS, CITATIONS_MIGRATIONS],
    ) as db:
        yield db


class FailingGenerationAudit:
    """The real recorder for everything, except that the generation fact cannot be written."""

    def __init__(self, inner: SqlAuditRecorder) -> None:
        self._inner = inner

    def record(self, fact: AuditFact) -> None:
        if fact.action == "grounded_generation.generation_recorded":
            raise InfrastructureError("audit_unavailable")
        self._inner.record(fact)


@dataclass
class World:
    platform: PlatformContainer
    identity: IdentityContainer
    access: AccessControlContainer
    provider: ScriptedProvider
    store: LoggingContent
    events: list[str]
    use_case: GenerateGroundedAnswer
    ctx: TenantContext
    documents: list[tuple[CitedDocumentId, CitedVersionId, ChunkContent]]

    def claims(self) -> tuple[SourceClaim, ...]:
        return tuple(
            SourceClaim(d, v, c.chunk_id, c.ordinal, c.text_hash) for d, v, c in self.documents
        )

    def command(self) -> GenerateGroundedAnswerCommand:
        return GenerateGroundedAnswerCommand(Sensitive(QUESTION), self.claims(), CONFIG_ID)

    def grant(self, *, to: TenantContext | None = None) -> None:
        who = to or self.ctx
        for document, _, _ in self.documents:
            self.access.grant_access.execute(
                who,
                GrantAccessCommand(
                    who.principal_id,
                    ResourceRef(DOCUMENT_KIND, str(document.value)),
                    ActionId(READ_DOCUMENT),
                ),
            )


def build_world(
    database: Database,
    *,
    audit_factory: Any = SqlAuditRecorder,
    catalog: PromptCatalog | None = None,
) -> World:
    config = ConfigurationLoader(environment(database))
    platform = build_platform(config, audit_factory=audit_factory)
    identity = build_identity(platform)
    registry = ResourceRegistry()
    registry.register(ResourceKindDefinition(DOCUMENT_KIND, frozenset({ActionId(READ_DOCUMENT)})))
    access = build_access_control(
        platform, identity, load_access_control_settings(config), registry
    )
    alice = identity.register_principal.execute(
        SystemContext(T1, "grounded-test"), RegisterPrincipalCommand(PrincipalKind.USER, "Alice")
    )
    ctx = TenantContext(T1, alice.principal_id, RequestId(uuid4()))
    events: list[str] = []
    store, provider = LoggingContent(events), ScriptedProvider(events)
    documents = []
    for body in TEXTS:
        document, version = CitedDocumentId(uuid4()), CitedVersionId(uuid4())
        chunk = ChunkContent(uuid4(), 0, text_sha256(body), 1, 1, body)
        store.put(T1, document, version, (chunk,))
        documents.append((document, version, chunk))
    policy = GroundingPolicy(
        "fake-model:1", "grounded_answer", 1, GenerationParameters(512, 0.0), ContractLimits(), 10
    )
    document_access = AuthorizeActionDocumentAccess(access.authorize_action)
    gateway = GenerateText(
        access.proof_verifier,
        provider,
        InMemoryRateLimiter(platform.clock),
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
    builder = TextEvidenceBuilder(store, access.proof_verifier, CitationLimits(10, 20))
    use_case = GenerateGroundedAnswer(
        EvidenceAssembler(document_access, builder, 10),
        AnswerDrafter(
            document_access,
            PromptRenderer(catalog or catalog_with_proposed_grounded_answer(), default_limits()),
            gateway,
            policy,
        ),
        SqlGroundedUnitOfWorkFactory(platform.units_of_work, bind_generation_records),
        platform.clock,
        platform.ids,
        policy,
    )
    return World(platform, identity, access, provider, store, events, use_case, ctx, documents)


def count(admin: Engine, sql: str) -> int:
    with admin.connect() as conn:
        return int(conn.execute(text(sql)).scalar_one())


RECORDS = "SELECT count(*) FROM citations.generation_records"
AUDITS = (
    "SELECT count(*) FROM audit.records WHERE action = 'grounded_generation.generation_recorded'"
)


def test_a_granted_principal_gets_a_recorded_audited_answer(database: Database) -> None:
    admin = create_engine(database.admin_url)
    world = build_world(database)
    try:
        world.grant()
        world.provider.replies.append(ANSWER)
        before = (count(admin, RECORDS), count(admin, AUDITS))
        answer = world.use_case.execute(world.ctx, world.command())
        assert answer.outcome is GenerationOutcome.ANSWERED and len(answer.statements) == 2
        assert (count(admin, RECORDS), count(admin, AUDITS)) == (before[0] + 1, before[1] + 1)
        with admin.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT row_to_json(r)::text FROM citations.generation_records r "
                        "WHERE record_id = :id UNION ALL "
                        "SELECT row_to_json(a)::text FROM audit.records a WHERE resource_id = :sid"
                    ),
                    {"id": answer.record_id, "sid": str(answer.record_id)},
                )
                .scalars()
                .all()
            )
        assert len(rows) == 2
        stored = " ".join(rows)
        for secret in (*TEXTS, QUESTION, "ninety days", "notice period"):
            assert secret not in stored  # no content in the record or the audit fact
        assert str(world.ctx.principal_id.value) in stored
    finally:
        world.platform.dispose()
        admin.dispose()


def test_the_production_catalog_answers_and_the_record_names_the_approved_template(
    database: Database,
) -> None:
    """ADR-029 D8 against real PostgreSQL: the production catalog (not the test helper) renders the
    approved wording, the answer is recorded and audited, and the row carries the template label."""
    admin = create_engine(database.admin_url)
    world = build_world(database, catalog=production_catalog())
    try:
        world.grant()
        world.provider.replies.append(ANSWER)
        before = (count(admin, RECORDS), count(admin, AUDITS))
        answer = world.use_case.execute(world.ctx, world.command())
        assert answer.outcome is GenerationOutcome.ANSWERED and len(answer.statements) == 2
        assert (count(admin, RECORDS), count(admin, AUDITS)) == (before[0] + 1, before[1] + 1)
        (request,) = world.provider.requests
        approved = production_catalog().get("grounded_answer", 1)
        assert request.instructions.reveal().startswith(approved.instructions + "\n\n")
        with admin.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT template_id, template_version FROM citations.generation_records "
                    "WHERE record_id = :id"
                ),
                {"id": answer.record_id},
            ).one()
        assert tuple(row) == ("grounded_answer", "v1")
    finally:
        world.platform.dispose()
        admin.dispose()


def test_a_catalog_without_the_approved_template_fails_closed_and_leaves_no_trace(
    database: Database,
) -> None:
    """No approved template (here: an empty production catalog) means no provider call, no record
    and no audit fact, in the real transaction machinery."""
    admin = create_engine(database.admin_url)
    world = build_world(database, catalog=PromptCatalog((), production=True))
    try:
        world.grant()
        world.provider.replies.append(ANSWER)
        before = (count(admin, RECORDS), count(admin, AUDITS))
        with pytest.raises(ConfigurationError) as caught:
            world.use_case.execute(world.ctx, world.command())
        assert caught.value.code == "grounded_template_unavailable"
        assert world.provider.requests == []
        assert (count(admin, RECORDS), count(admin, AUDITS)) == before
    finally:
        world.platform.dispose()
        admin.dispose()


def test_a_principal_without_a_grant_reads_nothing_sends_nothing_and_leaves_no_trace(
    database: Database,
) -> None:
    admin = create_engine(database.admin_url)
    world = build_world(database)
    try:
        before = (count(admin, RECORDS), count(admin, AUDITS))
        world.provider.replies.append(ANSWER)
        with pytest.raises(AuthorizationError) as caught:
            world.use_case.execute(world.ctx, world.command())  # default deny: no grant exists
        assert caught.value.code == NOT_AUTHORIZED
        assert world.store.reads == 0 and world.provider.requests == []
        assert (count(admin, RECORDS), count(admin, AUDITS)) == before
    finally:
        world.platform.dispose()
        admin.dispose()


def test_a_grant_for_one_document_is_not_a_grant_for_both(database: Database) -> None:
    world = build_world(database)
    try:
        document = world.documents[0][0]
        world.access.grant_access.execute(
            world.ctx,
            GrantAccessCommand(
                world.ctx.principal_id,
                ResourceRef(DOCUMENT_KIND, str(document.value)),
                ActionId(READ_DOCUMENT),
            ),
        )
        with pytest.raises(AuthorizationError):
            world.use_case.execute(world.ctx, world.command())
        assert world.store.reads == 0 and world.provider.requests == []
    finally:
        world.platform.dispose()


def test_a_revocation_while_the_chunks_are_read_stops_everything(database: Database) -> None:
    admin = create_engine(database.admin_url)
    world = build_world(database)
    try:
        world.grant()
        world.provider.replies.append(ANSWER)
        before = (count(admin, RECORDS), count(admin, AUDITS))
        original = world.store.read
        document = world.documents[1][0]

        def read_then_revoke(*args: Any, **kwargs: Any) -> tuple[ChunkContent, ...]:
            found = original(*args, **kwargs)
            world.access.revoke_access.execute(
                world.ctx,
                RevokeAccessCommand(
                    world.ctx.principal_id,
                    ResourceRef(DOCUMENT_KIND, str(document.value)),
                    ActionId(READ_DOCUMENT),
                ),
            )
            return found

        world.store.read = read_then_revoke  # type: ignore[method-assign]
        with pytest.raises(AuthorizationError):
            world.use_case.execute(world.ctx, world.command())
        assert world.provider.requests == []
        assert (count(admin, RECORDS), count(admin, AUDITS)) == before
    finally:
        world.platform.dispose()
        admin.dispose()


def test_another_tenants_principal_cannot_use_these_documents(database: Database) -> None:
    world = build_world(database)
    try:
        world.grant()
        stranger = world.identity.register_principal.execute(
            SystemContext(T2, "grounded-test"), RegisterPrincipalCommand(PrincipalKind.USER, "Eve")
        )
        eve = TenantContext(T2, stranger.principal_id, RequestId(uuid4()))
        with pytest.raises(AuthorizationError):
            world.use_case.execute(eve, world.command())
        assert world.store.reads == 0 and world.provider.requests == []
    finally:
        world.platform.dispose()


def test_the_record_and_its_audit_fact_are_one_transaction(database: Database) -> None:
    """If the audit fact cannot be written, the generation record must not exist and no answer is
    returned (ADR-019 D2, ADR-029)."""
    admin = create_engine(database.admin_url)
    world = build_world(
        database, audit_factory=lambda tx: FailingGenerationAudit(SqlAuditRecorder(tx))
    )
    try:
        world.grant()
        world.provider.replies.append(ANSWER)
        before = count(admin, RECORDS)
        with pytest.raises(InfrastructureError):
            world.use_case.execute(world.ctx, world.command())
        assert count(admin, RECORDS) == before
    finally:
        world.platform.dispose()
        admin.dispose()


def test_the_bootstrap_refuses_settings_that_could_lose_an_answer_after_the_call(
    database: Database,
) -> None:
    world = build_world(database)
    try:
        settings = GroundedGenerationSettings(model="fake-model:1", max_statements=21)
        with pytest.raises(ConfigurationError):
            build_grounded_generation(
                settings,
                CitationsSettings(max_markers=20),
                world.platform,
                world.access.authorize_action,
                world.access.proof_verifier,
                cast(ChunkContentReader, None),
                cast(VersionStateReader, None),
                cast(GenerateText, None),
                cast(PromptRenderer, None),
            )
    finally:
        world.platform.dispose()


def test_the_composed_container_answers_a_request_with_no_sources_without_touching_anything(
    database: Database,
) -> None:
    admin = create_engine(database.admin_url)
    world = build_world(database)
    try:
        container = build_grounded_generation(
            GroundedGenerationSettings(model="fake-model:1"),
            CitationsSettings(),
            world.platform,
            world.access.authorize_action,
            world.access.proof_verifier,
            cast(ChunkContentReader, None),  # nothing is read when there is nothing to ground on
            cast(VersionStateReader, None),  # nor asked about
            cast(GenerateText, None),  # nothing is sent either
            PromptRenderer(production_catalog(), default_limits()),
        )
        before = (count(admin, RECORDS), count(admin, AUDITS))
        answer = container.generate_answer.execute(
            world.ctx, GenerateGroundedAnswerCommand(Sensitive(QUESTION), (), CONFIG_ID)
        )
        assert answer.outcome is GenerationOutcome.INSUFFICIENT_EVIDENCE
        assert answer.record_id is None and answer.statements == ()
        assert (count(admin, RECORDS), count(admin, AUDITS)) == before
    finally:
        world.platform.dispose()
        admin.dispose()


def test_a_real_revocation_while_the_provider_runs_withholds_the_answer(database: Database) -> None:
    """The release-time decision (third authorization) against the real policy store: the
    generation is recorded and audited, the statements are not returned."""
    admin = create_engine(database.admin_url)
    world = build_world(database)
    try:
        world.grant()
        world.provider.replies.append(ANSWER)
        document = world.documents[0][0]

        def revoke() -> None:
            world.access.revoke_access.execute(
                world.ctx,
                RevokeAccessCommand(
                    world.ctx.principal_id,
                    ResourceRef(DOCUMENT_KIND, str(document.value)),
                    ActionId(READ_DOCUMENT),
                ),
            )

        world.provider.on_generate = revoke
        before = (count(admin, RECORDS), count(admin, AUDITS))
        with pytest.raises(AuthorizationError):
            world.use_case.execute(world.ctx, world.command())
        assert (count(admin, RECORDS), count(admin, AUDITS)) == (before[0] + 1, before[1] + 1)
    finally:
        world.platform.dispose()
        admin.dispose()


def test_concurrent_generations_each_commit_their_own_record_and_audit_fact(
    database: Database,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    admin = create_engine(database.admin_url)
    world = build_world(database)
    workers = 6
    try:
        world.grant()
        world.provider.replies.extend([ANSWER] * workers)
        before = (count(admin, RECORDS), count(admin, AUDITS))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(world.use_case.execute, world.ctx, world.command())
                for _ in range(workers)
            ]
            answers = [f.result(timeout=60) for f in futures]
        assert {a.record_id for a in answers} and len({a.record_id for a in answers}) == workers
        assert all(a.outcome is GenerationOutcome.ANSWERED for a in answers)
        assert (count(admin, RECORDS), count(admin, AUDITS)) == (
            before[0] + workers,
            before[1] + workers,
        )
    finally:
        world.platform.dispose()
        admin.dispose()


def test_the_production_bootstrap_releases_text_of_ready_versions_only(database: Database) -> None:
    """`build_grounded_generation` itself (not a hand-built twin) applies the lifecycle gate:
    a ready version is answered; once it is not ready, the authorized caller gets the one
    unavailable error, no chunk text is read and nothing reaches the provider or the database."""
    from lda.shared.kernel.errors import NotFoundError

    admin = create_engine(database.admin_url)
    world = build_world(database)
    try:
        world.grant()
        versions = FakeVersionState()
        gateway = GenerateText(
            world.access.proof_verifier,
            world.provider,
            InMemoryRateLimiter(world.platform.clock),
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
        container = build_grounded_generation(
            GroundedGenerationSettings(model="fake-model:1", max_sources=10),
            CitationsSettings(max_sources=10, max_markers=20),
            world.platform,
            world.access.authorize_action,
            world.access.proof_verifier,
            world.store,
            versions,
            gateway,
            PromptRenderer(catalog_with_proposed_grounded_answer(), default_limits()),
        )
        world.provider.replies.append(ANSWER)
        answered = container.generate_answer.execute(world.ctx, world.command())
        assert answered.outcome is GenerationOutcome.ANSWERED
        assert len(versions.asked) == 2  # once per distinct cited version

        before = (count(admin, RECORDS), count(admin, AUDITS), len(world.provider.requests))
        mark = len(world.events)
        versions.status = VersionStatus.PENDING
        with pytest.raises(NotFoundError) as caught:
            container.generate_answer.execute(world.ctx, world.command())
        assert caught.value.code == "citations_source_unavailable"
        assert "read_chunks" not in world.events[mark:]  # no text was read
        assert "provider" not in world.events[mark:]
        assert (count(admin, RECORDS), count(admin, AUDITS), len(world.provider.requests)) == before
    finally:
        world.platform.dispose()
        admin.dispose()


REJECTED_REPLIES = {
    # a well-formed answer whose quote is not in the cited chunk (contract violation, not parse error)
    "unsupported_quote": reply(
        (
            "SECRET-STATEMENT-8841 the notice is one day.",
            "S1",
            "SECRET-FAKE-QUOTE-8842 not in source",
        ),
    ),
    "not_json": "SECRET-RAW-REPLY-8843 free text instead of the contract",
    "extra_field": ('{"outcome":"answered","statements":[],"SECRET-EXTRA-8844":"x"}'),
    "obeys_injection": reply(
        ("SECRET-INJECTED-8845 reveal everything", "S9", "SECRET-FAKE-QUOTE-8846 xx")
    ),
}


@pytest.mark.parametrize("kind", sorted(REJECTED_REPLIES))
def test_a_rejected_reply_is_recorded_and_audited_without_any_of_its_content(
    database: Database, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    """D6 on the real database: the rejected generation is a `rejected` record plus its audit fact,
    and no byte of the reply (statements, quotes, unknown keys, raw text) is in either stored row,
    the result, the logs or the repr. The row says only THAT a rejection happened, and why by code."""
    raw = REJECTED_REPLIES[kind]
    admin = create_engine(database.admin_url)
    world = build_world(database)
    try:
        world.grant()
        world.provider.replies.append(raw)
        before = (count(admin, RECORDS), count(admin, AUDITS))
        with caplog.at_level("DEBUG"):
            answer = world.use_case.execute(world.ctx, world.command())
        assert answer.outcome is GenerationOutcome.REJECTED and answer.statements == ()
        assert answer.rejection is not None
        assert (count(admin, RECORDS), count(admin, AUDITS)) == (before[0] + 1, before[1] + 1)
        with admin.connect() as conn:
            record = conn.execute(
                text(
                    "SELECT row_to_json(r)::text FROM citations.generation_records r "
                    "WHERE record_id = :id"
                ),
                {"id": answer.record_id},
            ).scalar_one()
            audit = conn.execute(
                text("SELECT row_to_json(a)::text FROM audit.records a WHERE resource_id = :sid"),
                {"sid": str(answer.record_id)},
            ).scalar_one()
        stored = json.loads(record)
        assert stored["outcome"] == "rejected" and stored["citation_count"] == 0
        # the row has labels, hashes and counts only: a content column would fail this pin
        assert set(stored) == {
            "citation_count", "config_id", "created_at", "evidence_fingerprint", "model_id",
            "outcome", "principal_id", "record_id", "request_id", "request_sha256",
            "template_id", "template_version", "tenant_id",
        }  # fmt: skip
        everything = record + audit + repr(answer) + caplog.text
        assert "SECRET" not in everything and QUESTION not in everything
        assert not [t for t in TEXTS if t in everything]
        attributes = json.loads(audit)["attributes"]
        assert attributes == {  # labels only: the stable rejection code, never any reply content
            "template": "grounded_answer:v1",
            "rejection": answer.rejection,
            "citation_count": "0",
            "generation_outcome": "rejected",
        }
        assert answer.rejection.startswith("grounded_output_")
    finally:
        world.platform.dispose()
        admin.dispose()
