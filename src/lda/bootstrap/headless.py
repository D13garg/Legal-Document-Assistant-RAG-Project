"""DEVELOPMENT-ONLY headless vertical slice (ADR-045): composition and driver logic.

Runs the real use cases in this process (upload, authorized read, conversation, answering) while
the pipeline work (scan, extract, chunk, index) is done by a SEPARATE worker process that shares the
database and the S3-compatible object store. It writes nothing to domain tables itself, builds no
`ExecutionContext` of its own (the only trusted context is `DevOperator.tenant_context()`, ADR-043)
and reads documents only through the authorized interfaces.

The embedder is INJECTED. Until the owner rules on a development embedder (ADR-045), no adapter
set is registered and the command refuses; tests inject the retrieval test double.

Nothing here demonstrates semantic quality, malware scanning or a real language model.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import Field

from lda.bootstrap.access_control import (
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.ai_gateway import build_development_gateway, load_ai_gateway_settings
from lda.bootstrap.answering import AnsweringContainer, build_answering, load_answering_settings
from lda.bootstrap.audit import build_audit
from lda.bootstrap.chunking import build_chunk_reader, build_chunking, load_chunking_settings
from lda.bootstrap.citations import (
    ChunkContentReader,
    build_chunk_content_reader,
    build_generation_records,
    load_evidence_settings,
    read_cited_chunks,
)
from lda.bootstrap.container import build_platform
from lda.bootstrap.conversations import (
    ConversationsContainer,
    build_conversations,
    load_conversations_settings,
)
from lda.bootstrap.dev_operator import (
    DevOperator,
    DevOperatorSettings,
    load_dev_operator_settings,
)
from lda.bootstrap.development import DevelopmentScanner, require_development
from lda.bootstrap.documents import (
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
from lda.bootstrap.identity import build_identity
from lda.bootstrap.ingestion import build_ingestion, load_ingestion_settings
from lda.bootstrap.processing import (
    build_extraction_reader,
    build_processing,
    load_processing_settings,
)
from lda.bootstrap.prompts import GatewayBounds, build_rendering, load_rendering_settings
from lda.bootstrap.rag_configuration import bind_runtime_configuration, effective_configuration
from lda.bootstrap.retrieval import Embedder, build_index, load_index_settings
from lda.modules.access_control.api import ResourceRegistry
from lda.modules.answering.api import AnswerCommand, AnswerOutcome
from lda.modules.conversations.api import ClientMessageId, StartConversationCommand
from lda.modules.documents.api import (
    DocumentId,
    DocumentReference,
    UploadDocumentCommand,
    VersionStatus,
)
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.configuration.settings import ObjectStorageSettings, SettingsSlice
from lda.platform.storage.readiness import check_bucket_ready
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.errors import AuthorizationError, ConfigurationError, LdaError

_SAMPLE = (
    "The Supplier shall give ninety days written notice before terminating this agreement.\n\n"
    "Fees are payable within thirty days of the invoice date, without set-off."
)
SAMPLE_QUESTION = "what is the notice period?"


class DevSliceSettings(SettingsSlice):
    """Optional `LDA_DEV_SLICE__*`; both have safe defaults."""

    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    poll_seconds: float = Field(default=0.5, gt=0, le=10)


class SliceError(LdaError):
    """A stage of the slice failed. `code` names the stage; `detail` never holds content."""


@dataclass(frozen=True, slots=True)
class SliceReport:
    """Content-free result: identifiers and counts only."""

    document_id: UUID
    version_id: UUID
    outcome: str
    citations: int
    record_id: UUID | None
    other_tenant_denied: bool


@dataclass(frozen=True, slots=True)
class Headless:
    operator: DevOperator
    docs: DocsContainer
    conversations: ConversationsContainer
    answering: AnsweringContainer
    read_chunks: ChunkContentReader
    dispose: Callable[[], None]


def require_shared_object_store(config: ConfigurationLoader) -> None:
    """A workflow that spans processes needs a real S3-compatible endpoint and explicit keys."""
    store = config.load("object_storage", ObjectStorageSettings)
    if store.endpoint_url is None or store.access_key_id is None or store.secret_access_key is None:
        raise ConfigurationError(
            "shared_object_store_required",
            detail="the headless slice needs endpoint_url and explicit access keys",
        )
    check_bucket_ready(store)


def build_headless(
    config: ConfigurationLoader, embedder: Embedder, operator_settings: DevOperatorSettings
) -> Headless:
    """The document-to-answer composition for ONE tenant. The platform (pool) is disposed on any
    failure while building."""
    platform = build_platform(config, audit_factory=build_audit().recorder_factory)
    try:
        identity = build_identity(platform)
        registry = ResourceRegistry()
        register_doc_resources(registry)
        access = build_access_control(
            platform, identity, load_access_control_settings(config), registry
        )
        reader = build_original_reader(platform, access)
        processing = build_processing(platform, reader, load_processing_settings(config))
        versions = build_version_state_reader(platform, access)
        chunking_settings = load_chunking_settings(config)
        chunking = build_chunking(
            platform, build_extraction_reader(platform, versions), chunking_settings
        )
        read_chunks = build_chunk_reader(platform, versions)
        index_settings = load_index_settings(config)
        retrieval = build_index(
            platform,
            chunks=read_chunks,
            versions=versions,
            verifier=access.proof_verifier,
            embedder=embedder,
            settings=index_settings,
        )
        # The submit side only enqueues; the scanner and stages that really run live in the worker
        # process. The ingestion container is still needed to build the documents use cases.
        ingestion = build_ingestion(
            platform,
            load_ingestion_settings(config),
            scanner=DevelopmentScanner(),
            stages=[processing.stage, chunking.stage, retrieval.stage],
        )
        docs = build_docs(platform, access, ingestion, load_docs_settings(config))
        gateway_settings = load_ai_gateway_settings(config)
        gateway = build_development_gateway(platform, access.proof_verifier, gateway_settings)
        rendering = build_rendering(
            load_rendering_settings(config),
            GatewayBounds(
                gateway_settings.max_segments,
                gateway_settings.max_segment_characters,
                gateway_settings.max_instruction_characters,
                gateway_settings.max_request_characters,
            ),
        )
        grounded_settings = load_grounded_generation_settings(config)
        evidence_settings = load_evidence_settings(config)
        content = build_chunk_content_reader(read_chunks)
        generation = build_grounded_generation(
            grounded_settings,
            evidence_settings,
            platform,
            access.authorize_action,
            access.proof_verifier,
            content,
            versions,
            gateway.generate_text,
            rendering.renderer,
        )
        records = build_generation_records(platform)
        conversation_settings = load_conversations_settings(config)
        conversations = build_conversations(platform, conversation_settings, records)
        answering_settings = load_answering_settings(config)
        runtime = bind_runtime_configuration(
            effective_configuration(
                grounded=grounded_settings,
                index=index_settings,
                evidence=evidence_settings,
                chunking=chunking_settings,
                answering=answering_settings,
            ),
            answering_settings.config_id,
        )
        answering = build_answering(
            answering_settings,
            access.resolve_access_scope,
            retrieval.search,
            generation.generate_answer,
            conversations.append_user_message,
            conversations.append_assistant_turn,
            index=index_settings,
            grounded=grounded_settings,
            evidence=evidence_settings,
            conversations=conversation_settings,
            runtime=runtime,
        )
        return Headless(
            DevOperator(operator_settings, identity, access),
            docs,
            conversations,
            answering,
            content,
            platform.dispose,
        )
    except BaseException:
        platform.dispose()
        raise


def _wait_until_ready(
    stack: Headless,
    ctx_factory: Callable[[], TenantContext],
    resource: UUID,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> UUID:
    """Bounded polling of the REAL, authorized document read until the version is READY."""
    deadline = monotonic() + timeout_seconds
    while True:
        view = stack.docs.get_document.execute(
            ctx_factory(), DocumentReference(_resource_ref(resource))
        )
        latest = view.versions[0]
        if latest.status == VersionStatus.READY.value:
            return UUID(str(latest.version_id.value))
        if latest.status == VersionStatus.FAILED.value:
            raise SliceError("slice_processing_failed")
        if latest.status == VersionStatus.QUARANTINED.value:
            raise SliceError("slice_document_quarantined")
        if monotonic() >= deadline:
            raise SliceError("slice_timeout", detail="document did not reach ready in time")
        sleep(poll_seconds)


def _resource_ref(value: UUID) -> DocumentId:
    return DocumentId(value)


def run_slice(
    config: ConfigurationLoader,
    embedder: Embedder,
    *,
    body: str = _SAMPLE,
    question: str = SAMPLE_QUESTION,
    timeout_seconds: float = 60.0,
    poll_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SliceReport:
    """Upload, wait for the separate worker, answer, verify, and show tenant isolation.

    Raises `SliceError` (stage code, no content) on a failed or timed-out stage. Disposes every
    pool it opened. Never writes a domain row itself.
    """
    require_development(config)
    require_shared_object_store(config)
    settings = load_dev_operator_settings(config)
    first = build_headless(config, embedder, settings)
    others: list[Headless] = []
    try:
        registered = first.operator.ensure_principal()
        operator = DevOperator(
            settings.model_copy(update={"principal_id": registered.principal_id.value}),
            first.operator._identity,  # noqa: SLF001 - same stack, same containers
            first.operator._access,  # noqa: SLF001
        )
        ctx = operator.tenant_context()
        view = first.docs.upload_document.execute(
            ctx, UploadDocumentCommand("Development sample", "text/plain", body.encode())
        )
        document = UUID(str(view.document_id.value))
        operator.grant_first_read(document)
        version = _wait_until_ready(
            first,
            operator.tenant_context,
            document,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            sleep=sleep,
            monotonic=monotonic,
        )
        conversation = first.conversations.start_conversation.execute(
            operator.tenant_context(), StartConversationCommand()
        ).conversation_id
        turn = first.answering.answer.execute(
            operator.tenant_context(),
            AnswerCommand(conversation, ClientMessageId(uuid4()), question, (document,)),
        )
        if turn.outcome is not AnswerOutcome.ANSWERED:
            raise SliceError("slice_not_answered", detail=str(turn.outcome))
        if turn.record_id is None or turn.assistant_message is None or not turn.statements:
            raise SliceError("slice_provenance_missing")
        stored = {
            c.chunk_id: c
            for c in read_cited_chunks(first.read_chunks, operator.tenant_id, document, version)
        }
        for statement in turn.statements:
            source = statement.citation.source
            chunk = stored.get(source.chunk_id)
            if (
                chunk is None
                or chunk.text_hash != source.text_hash
                or source.document_id.value != document
                or statement.text.reveal() not in chunk.text
            ):
                raise SliceError("slice_citation_unresolved")
        denied = _other_tenant_is_denied(config, embedder, settings, document, others)
        return SliceReport(
            document, version, turn.outcome.value, len(turn.statements), turn.record_id, denied
        )
    finally:
        for extra in others:
            extra.dispose()
        first.dispose()


def _other_tenant_is_denied(
    config: ConfigurationLoader,
    embedder: Embedder,
    settings: DevOperatorSettings,
    resource: UUID,
    opened: list[Headless],
) -> bool:
    """A second, real tenant: its own principal, its own conversation, the first tenant's id."""
    other_settings = settings.model_copy(update={"tenant_id": uuid4(), "principal_id": None})
    other = build_headless(config, embedder, other_settings)
    opened.append(other)
    registered = other.operator.ensure_principal()
    operator = DevOperator(
        other_settings.model_copy(update={"principal_id": registered.principal_id.value}),
        other.operator._identity,  # noqa: SLF001
        other.operator._access,  # noqa: SLF001
    )
    ctx = operator.tenant_context()
    conversation = other.conversations.start_conversation.execute(
        ctx, StartConversationCommand()
    ).conversation_id
    try:
        other.answering.answer.execute(
            operator.tenant_context(),
            AnswerCommand(conversation, ClientMessageId(uuid4()), SAMPLE_QUESTION, (resource,)),
        )
    except AuthorizationError:
        pass
    else:
        raise SliceError("slice_isolation_breach")
    try:
        other.docs.get_document.execute(
            operator.tenant_context(), DocumentReference(_resource_ref(resource))
        )
    except LdaError:
        return True
    raise SliceError("slice_isolation_breach")


def run_command(config: ConfigurationLoader) -> int:
    """`python -m apps.dev_slice`. Exit 0 verified, 1 a stage failed, 2 refused before any work.

    Prints identifiers and counts only. With no registered adapter set (ADR-045: the embedder
    ruling is pending) it refuses, so it can never run on a silent fake.
    """
    import sys

    from lda.bootstrap.worker import select_adapters

    try:
        timing = config.load("dev_slice", DevSliceSettings)
        report = run_slice(
            config,
            select_adapters(config).embedder,
            timeout_seconds=timing.timeout_seconds,
            poll_seconds=timing.poll_seconds,
        )
    except SliceError as exc:
        print(f"dev_slice: failed: {exc.code}", file=sys.stderr)
        return 1
    except LdaError as exc:
        print(f"dev_slice: refused: {exc.code} {exc.detail}".rstrip(), file=sys.stderr)
        return 2
    print(f"document {report.document_id} version {report.version_id}")
    print(f"outcome {report.outcome}; citations {report.citations}; record {report.record_id}")
    print(f"other tenant denied: {report.other_tenant_denied}")
    return 0
