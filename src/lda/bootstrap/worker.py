"""Worker process composition and lifecycle (ADR-044). `apps/worker.py` is a thin shell around it.

Wires the document-to-index half of the pipeline with the existing builders, in the order the
answering end-to-end test proves, merges their job handlers (refusing duplicate keys), and runs
the existing `JobWorker.run_until`. Required queues are enforced by the builders themselves
(`require_queues`, plus ingestion's equivalent inline check); nothing here bypasses them.

The scanner and the embedder are injected as `WorkerAdapters`. There is NO default: the process
selects a registered adapter set by name, and the registry is empty until the development
adapters exist (Step 20C), so the command refuses rather than run with a fake.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import FrameType, MappingProxyType
from uuid import uuid4

from pydantic import Field

from lda.bootstrap.access_control import (
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.audit import build_audit
from lda.bootstrap.chunking import build_chunk_reader, build_chunking, load_chunking_settings
from lda.bootstrap.container import build_platform
from lda.bootstrap.development import ADAPTER_SET, development_embedder, development_scanner
from lda.bootstrap.documents import (
    build_docs,
    build_original_reader,
    build_version_state_reader,
    load_docs_settings,
    register_doc_resources,
)
from lda.bootstrap.identity import build_identity
from lda.bootstrap.ingestion import build_ingestion, load_ingestion_settings
from lda.bootstrap.processing import (
    build_extraction_reader,
    build_processing,
    load_processing_settings,
)
from lda.bootstrap.retrieval import Embedder, build_index, load_index_settings
from lda.modules.access_control.api import ResourceRegistry
from lda.modules.ingestion.api import ContentScanner
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.configuration.settings import LoggingSettings, SettingsSlice
from lda.platform.database.jobs.worker import BackoffPolicy, JobWorker
from lda.platform.telemetry.logging import configure_logging
from lda.shared.kernel.errors import ConfigurationError, LdaError
from lda.shared.ports.jobs import JobHandler

log = logging.getLogger("lda.worker")


class WorkerSettings(SettingsSlice):
    """`LDA_WORKER__ADAPTER_SET`: required, no default (ADR-044)."""

    adapter_set: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class WorkerAdapters:
    scanner: ContentScanner
    embedder: Embedder


def development_adapters(config: ConfigurationLoader) -> WorkerAdapters:
    """The one registered set. Each factory runs `require_development` FIRST, so selecting it
    without the explicit development environment (or off a local database) fails closed."""
    return WorkerAdapters(development_scanner(config), development_embedder(config))


# Immutable at run time and never filled from the environment. The only entry is the development
# set (ADR-045); any other name, and the development name outside development, is refused.
ADAPTER_SETS: Mapping[str, Callable[[ConfigurationLoader], WorkerAdapters]] = MappingProxyType(
    {ADAPTER_SET: development_adapters}
)


def select_adapters(config: ConfigurationLoader) -> WorkerAdapters:
    name = config.load("worker", WorkerSettings).adapter_set
    factory = ADAPTER_SETS.get(name)
    if factory is None:
        raise ConfigurationError("worker_adapter_set_unavailable")
    return factory(config)


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    worker: JobWorker
    handler_types: tuple[str, ...]
    queues: tuple[str, ...]
    dispose: Callable[[], None]


def merge_handlers(*sources: Mapping[str, JobHandler]) -> dict[str, JobHandler]:
    """A plain dict merge would let a later container silently replace an earlier handler."""
    merged: dict[str, JobHandler] = {}
    for source in sources:
        for key, handler in source.items():
            if key in merged:
                raise ConfigurationError("duplicate_job_handler", detail=key)
            merged[key] = handler
    return merged


def build_worker_runtime(
    config: ConfigurationLoader, adapters: WorkerAdapters, *, worker_id: str | None = None
) -> WorkerRuntime:
    """Validate every slice, build the pipeline, return the worker. Disposes the pool on failure."""
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
        chunking = build_chunking(
            platform, build_extraction_reader(platform, versions), load_chunking_settings(config)
        )
        retrieval = build_index(
            platform,
            chunks=build_chunk_reader(platform, versions),
            versions=versions,
            verifier=access.proof_verifier,
            embedder=adapters.embedder,
            settings=load_index_settings(config),
        )
        ingestion = build_ingestion(
            platform,
            load_ingestion_settings(config),
            scanner=adapters.scanner,
            stages=[processing.stage, chunking.stage, retrieval.stage],
        )
        docs = build_docs(platform, access, ingestion, load_docs_settings(config))
        handlers = merge_handlers(
            docs.job_handlers,
            ingestion.job_handlers,
            processing.job_handlers,
            chunking.job_handlers,
            retrieval.job_handlers,
        )
        settings = platform.job_settings
        worker = JobWorker(
            platform.job_leases,
            handlers,
            settings,
            platform.clock,
            worker_id or f"worker-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}",
            BackoffPolicy(settings.backoff_base_seconds, settings.backoff_cap_seconds),
            platform.job_maintenance,
        )
        return WorkerRuntime(
            worker, tuple(sorted(handlers)), tuple(sorted(settings.queues)), platform.dispose
        )
    except BaseException:
        platform.dispose()
        raise


def _code(exc: BaseException) -> str:
    return exc.code if isinstance(exc, LdaError) else "unhandled_exception"


def run_worker(
    config: ConfigurationLoader,
    adapters: WorkerAdapters | None = None,
    *,
    stop: threading.Event | None = None,
    install_signals: bool = True,
) -> int:
    """Exit status: 0 clean stop, 1 failed while running, 2 refused at start-up.

    SIGINT/SIGTERM only set the stop event (the loop then claims no new work and returns after
    the in-flight batch, existing `JobWorker` semantics). Previous handlers are restored and the
    connection pool is always disposed. Nothing logged here contains a secret or document content.
    """
    stop_event = stop if stop is not None else threading.Event()
    previous: dict[signal.Signals, signal._HANDLER] = {}
    if install_signals:

        def request_stop(_signum: int, _frame: FrameType | None) -> None:
            stop_event.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
    try:
        try:
            configure_logging(config.load("logging", LoggingSettings))
            runtime = build_worker_runtime(config, adapters or select_adapters(config))
        except LdaError as exc:
            print(f"worker: refused: {exc.code} {exc.detail}".rstrip(), file=sys.stderr)
            return 2
        try:
            log.info(
                "worker_started",
                extra={
                    "fields": {
                        "queues": list(runtime.queues),
                        "handlers": len(runtime.handler_types),
                    }
                },
            )
            runtime.worker.run_until(stop_event)
            log.info("worker_stopped")
            return 0
        except Exception as exc:
            log.warning("worker_failed", extra={"fields": {"error_code": _code(exc)}})
            print(f"worker: failed: {_code(exc)}", file=sys.stderr)
            return 1
        finally:
            runtime.dispose()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
