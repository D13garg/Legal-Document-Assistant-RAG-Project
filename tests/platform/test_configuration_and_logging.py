from __future__ import annotations

import io
import json
import logging

import pytest

from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.configuration.settings import DatabaseSettings, JobWorkerSettings, LoggingSettings
from lda.platform.telemetry.logging import JsonLogFormatter, bind_log_context
from lda.shared.kernel.errors import ConfigurationError, NotFoundError


def test_loads_typed_slices_from_prefixed_variables() -> None:
    loader = ConfigurationLoader(
        {
            "LDA_DATABASE__URL": "postgresql+psycopg://u:p@h/db",
            "LDA_DATABASE__POOL_SIZE": "3",
            "LDA_JOBS__QUEUES": '{"ocr": 2}',
            "UNRELATED": "x",
        }
    )
    db = loader.load("database", DatabaseSettings)
    assert db.pool_size == 3 and "p@h" not in repr(db)  # SecretStr is masked
    assert loader.load("jobs", JobWorkerSettings).queues == {"ocr": 2}


def test_invalid_configuration_names_fields_but_never_values() -> None:
    loader = ConfigurationLoader({"LDA_DATABASE__POOL_SIZE": "not-a-number-SECRET"})
    with pytest.raises(ConfigurationError) as exc:
        loader.load("database", DatabaseSettings)
    assert "url" in exc.value.detail and "SECRET" not in exc.value.detail


def test_defaults_apply() -> None:
    assert ConfigurationLoader({}).load("logging", LoggingSettings).level == "INFO"


def emit(**kwargs: object) -> dict[str, object]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("test.logging")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info("job_finished", **kwargs)  # type: ignore[arg-type]
    return json.loads(stream.getvalue())


def test_log_fields_are_allow_listed() -> None:
    entry = emit(extra={"fields": {"queue": "ocr", "document_text": "CONFIDENTIAL"}})
    assert entry["queue"] == "ocr" and entry["dropped_fields"] == 1
    assert "CONFIDENTIAL" not in json.dumps(entry)


def test_log_context_binding_and_validation() -> None:
    with bind_log_context(request_id="r1", tenant_id="t1"):
        entry = emit()
    assert entry["request_id"] == "r1" and entry["tenant_id"] == "t1"
    with pytest.raises(ValueError), bind_log_context(document="x"):
        pass


def test_exception_text_is_never_logged() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("test.exc")
    logger.handlers[:] = [handler]
    logger.propagate = False
    try:
        raise NotFoundError("document_not_found", detail="PRIVILEGED CONTENT")
    except NotFoundError:
        logger.exception("failed")
    out = stream.getvalue()
    assert "PRIVILEGED" not in out and "document_not_found" in out and "Traceback" not in out
