"""Every module that enqueues work refuses a configuration that would never run it (Step 18).

`JobWorker` claims only the queues named in `LDA_JOBS__QUEUES`. A job enqueued on any other queue
is never claimed and never dead-lettered: it strands silently. `build_ingestion` already refuses
that for its own queue; these tests pin the same fail-fast rule for the queues the Documents,
Document Processing, Chunking and Retrieval compositions enqueue onto.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from lda.bootstrap.audit import AUDIT_MIGRATIONS
from lda.bootstrap.chunking import build_chunking, load_chunking_settings
from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.documents import build_docs, load_docs_settings
from lda.bootstrap.processing import build_processing, load_processing_settings
from lda.bootstrap.retrieval import build_index, load_index_settings
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError
from platform_support import Database, provisioned_database

_STUB = cast(Any, object())  # never touched: the queue check runs before any dependency is used


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database("lda_test_queue_cfg", "lda_app_queue_cfg", [AUDIT_MIGRATIONS]) as db:
        yield db


def platform_for(database: Database, queues: str) -> tuple[PlatformContainer, ConfigurationLoader]:
    config = ConfigurationLoader(
        {
            "LDA_DATABASE__URL": database.app_url,
            "LDA_OBJECT_STORAGE__BUCKET": "bucket",
            "LDA_JOBS__QUEUES": queues,
        }
    )
    return build_platform(config, audit_factory=SqlAuditRecorder), config


def attempt_documents(p: PlatformContainer, c: ConfigurationLoader) -> object:
    return build_docs(p, _STUB, _STUB, load_docs_settings(c))


def attempt_processing(p: PlatformContainer, c: ConfigurationLoader) -> object:
    return build_processing(p, _STUB, load_processing_settings(c))


def attempt_chunking(p: PlatformContainer, c: ConfigurationLoader) -> object:
    return build_chunking(p, _STUB, load_chunking_settings(c))


def attempt_retrieval(p: PlatformContainer, c: ConfigurationLoader) -> object:
    return build_index(
        p,
        chunks=_STUB,
        versions=_STUB,
        verifier=_STUB,
        embedder=_STUB,
        settings=load_index_settings(c),
    )


CASES = [
    # (builder, queues configured, expected error code)
    (attempt_documents, '{"events": 1, "ingestion": 1}', "documents_queue_not_configured"),
    (attempt_documents, '{"documents": 1, "ingestion": 1}', "events_queue_not_configured"),
    (attempt_processing, '{"documents": 1, "ingestion": 1}', "events_queue_not_configured"),
    (attempt_chunking, '{"documents": 1, "ingestion": 1}', "events_queue_not_configured"),
    (attempt_retrieval, '{"documents": 1, "ingestion": 1}', "events_queue_not_configured"),
]


@pytest.mark.parametrize(("attempt", "queues", "code"), CASES)
def test_a_missing_queue_is_refused_at_startup(
    database: Database, attempt: Any, queues: str, code: str
) -> None:
    platform, config = platform_for(database, queues)
    try:
        with pytest.raises(ConfigurationError, match=code):
            attempt(platform, config)
    finally:
        platform.dispose()
