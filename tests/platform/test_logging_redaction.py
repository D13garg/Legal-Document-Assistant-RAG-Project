"""Logs cannot carry content, credentials or exception text, whatever the call site does."""

from __future__ import annotations

import ast
import io
import json
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import boto3
import pytest
from moto import mock_aws
from pydantic import SecretStr

from lda.platform.configuration.settings import LoggingSettings, ObjectStorageSettings
from lda.platform.storage.s3 import S3ObjectStore
from lda.platform.telemetry.logging import (
    EVENT_NAME,
    JsonLogFormatter,
    bind_log_context,
    configure_logging,
)
from lda.shared.kernel.errors import NotFoundError
from lda.shared.kernel.ids import TenantId
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.object_store import TenantScopedKey

CANARY = "PRIVILEGED-CANARY-77"
SRC = Path(__file__).resolve().parents[2] / "src" / "lda"


def capture(logger_name: str = "lda.test") -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger(logger_name)
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def entry(stream: io.StringIO) -> dict[str, object]:
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    parsed: dict[str, object] = json.loads(lines[0])
    return parsed


# ---- messages are static event names -------------------------------------------------------------


def test_event_name_messages_pass_through() -> None:
    logger, stream = capture()
    logger.info("job_lease_lost")
    assert entry(stream)["msg"] == "job_lease_lost"


@pytest.mark.parametrize(
    "build",
    [
        lambda log: log.info(f"failed for {CANARY}"),
        lambda log: log.info("failed for %s", CANARY),
        lambda log: log.info("failed_for_%s", CANARY),
        lambda log: log.info("Contains Spaces " + CANARY),
        lambda log: log.info(CANARY.lower() + "\nforged line"),
        lambda log: log.info(ValueError(CANARY)),
        lambda log: log.info(Sensitive(CANARY)),
        lambda log: log.info(SecretStr(CANARY)),
    ],
)
def test_non_event_messages_are_replaced_and_args_are_never_interpolated(
    build: object,
) -> None:
    logger, stream = capture()
    build(logger)  # type: ignore[operator]
    out = stream.getvalue()
    assert CANARY not in out and CANARY.lower() not in out
    assert entry(stream)["msg"] == "invalid_log_message"


def test_third_party_loggers_never_emit_message_text() -> None:
    logger, stream = capture("botocore.endpoint")
    logger.warning("Authorization: AWS4-HMAC-SHA256 Signature=%s", CANARY)
    out = entry(stream)
    assert out["msg"] == "third_party_log" and out["logger"] == "botocore.endpoint"
    assert CANARY not in stream.getvalue() and "Authorization" not in stream.getvalue()


def test_a_logger_merely_prefixed_lda_is_not_trusted() -> None:
    logger, stream = capture("ldaevil")
    logger.info("looks_like_an_event")
    assert entry(stream)["msg"] == "third_party_log"


@pytest.fixture
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level


def test_debug_level_does_not_expose_provider_sdk_internals(
    restore_root_logging: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIACANARYKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "CANARYSECRET")
    configure_logging(LoggingSettings(level="DEBUG"))
    body = CANARY.encode()
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        store = S3ObjectStore(client, ObjectStorageSettings(bucket="test-bucket"))
        key = TenantScopedKey(TenantId(UUID(int=1)), "documents/d1/original")
        store.put(key, body, content_type="text/plain")
        store.get(key)
        store.presign_get(key, expires_seconds=60)
    out = capsys.readouterr().out
    assert out.strip(), "debug logging should have produced third-party records"
    for forbidden in (
        CANARY,
        "AKIACANARYKEY",
        "CANARYSECRET",
        "Authorization",
        "Signature",
        "tenant/",
    ):
        assert forbidden not in out
    assert {json.loads(line)["msg"] for line in out.splitlines()} == {"third_party_log"}


# ---- structured fields -----------------------------------------------------------------------------


def test_only_allow_listed_fields_survive_and_secrets_in_them_stay_masked() -> None:
    logger, stream = capture()
    logger.info(
        "job_finished",
        extra={
            "fields": {
                "queue": "ocr",
                "document_text": CANARY,
                "password": CANARY,
                "error_code": Sensitive(CANARY),
                "handler": SecretStr(CANARY),
            }
        },
    )
    out = entry(stream)
    assert out["queue"] == "ocr" and out["dropped_fields"] == 2
    assert CANARY not in stream.getvalue()


def test_extra_attributes_outside_the_fields_mapping_are_ignored() -> None:
    logger, stream = capture()
    logger.info("job_finished", extra={"document_text": CANARY, "fields": "not-a-mapping"})
    assert CANARY not in stream.getvalue()


# ---- exceptions ------------------------------------------------------------------------------------


def test_exceptions_log_type_and_code_only_never_text_cause_or_stack() -> None:
    logger, stream = capture()
    try:
        try:
            raise ValueError(f"inner {CANARY}")
        except ValueError as inner:
            raise NotFoundError("document_not_found", detail=f"detail {CANARY}") from inner
    except NotFoundError:
        logger.exception("failed", stack_info=True)
    out = entry(stream)
    assert out["exc_type"] == "NotFoundError" and out["error_code"] == "document_not_found"
    assert CANARY not in stream.getvalue() and "Traceback" not in stream.getvalue()
    assert "stack" not in stream.getvalue().lower().replace("exc_type", "")


def test_non_lda_exceptions_log_only_their_type() -> None:
    logger, stream = capture()
    try:
        raise KeyError(CANARY)
    except KeyError:
        logger.exception("failed")
    out = entry(stream)
    assert out["exc_type"] == "KeyError" and "error_code" not in out
    assert CANARY not in stream.getvalue()


# ---- context binding -------------------------------------------------------------------------------


def test_context_is_scoped_and_not_inherited_by_new_threads() -> None:
    logger, stream = capture()
    seen_in_thread: list[dict[str, object]] = []

    def in_thread() -> None:
        thread_logger, thread_stream = capture("lda.thread")
        thread_logger.info("from_thread")
        seen_in_thread.append(entry(thread_stream))

    with bind_log_context(request_id="r1"):
        logger.info("inside")
        threading.Thread(target=in_thread).start()
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.name.startswith("Thread"):
                t.join(5)
    logger.info("outside")
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["request_id"] == "r1" and "request_id" not in lines[1]
    assert "request_id" not in seen_in_thread[0]


# ---- call sites in the repository ------------------------------------------------------------------

LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}


def test_every_log_call_in_src_uses_a_literal_event_name() -> None:
    offenders: list[str] = []
    calls = 0
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LEVELS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"log", "logger", "logging"}
            ):
                continue
            calls += 1
            first = node.args[0] if node.args else None
            literal = isinstance(first, ast.Constant) and isinstance(first.value, str)
            if not (literal and EVENT_NAME.fullmatch(first.value)):  # type: ignore[union-attr]
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert calls >= 3, "the scan should have found the platform's log calls"
    assert offenders == []
