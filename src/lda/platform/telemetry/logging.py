"""Structured JSON logging that cannot carry document content.

Rules (ADR-020): messages are static event names; only allow-listed structured fields are emitted;
exception text and tracebacks are never emitted (only the exception type / stable error code).
The formatter enforces the first rule instead of trusting call sites: a message that is not an
event name (e.g. an interpolated f-string or `%s` template) is replaced, `args` are never
interpolated, and records from third-party loggers (botocore, urllib3, ...) carry no message text.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from lda.platform.configuration.settings import LoggingSettings
from lda.shared.kernel.errors import LdaError

FIELD_ALLOWLIST = frozenset(
    {
        "event",
        "duration_ms",
        "count",
        "queue",
        "job_type",
        "job_id",
        "status",
        "attempt",
        "error_code",
        "delivery",
        "handler",
        "bucket",
        "operation",
        "outcome",
        "stage",
        "run_status",
    }
)
EVENT_NAME = re.compile(r"[a-z][a-z0-9_.]{0,79}")
OWN_LOGGER_ROOT = "lda"
CONTEXT_KEYS = ("request_id", "tenant_id", "principal_id", "job_id")
_context: ContextVar[dict[str, str]] = ContextVar("lda_log_context", default={})  # noqa: B039


@contextmanager
def bind_log_context(**values: str) -> Iterator[None]:
    unknown = set(values) - set(CONTEXT_KEYS)
    if unknown:
        raise ValueError(f"unsupported log context keys: {sorted(unknown)}")
    token = _context.set({**_context.get(), **values})
    try:
        yield
    finally:
        _context.reset(token)


def _message(record: logging.LogRecord) -> str:
    own = record.name == OWN_LOGGER_ROOT or record.name.startswith(OWN_LOGGER_ROOT + ".")
    if not own:
        return "third_party_log"
    if isinstance(record.msg, str) and EVENT_NAME.fullmatch(record.msg):
        return record.msg
    return "invalid_log_message"


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _message(record),
        }
        entry.update(_context.get())
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            allowed = {k: v for k, v in fields.items() if k in FIELD_ALLOWLIST}
            entry.update(allowed)
            if len(allowed) != len(fields):
                entry["dropped_fields"] = len(fields) - len(allowed)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            entry["exc_type"] = type(exc).__name__
            if isinstance(exc, LdaError):
                entry["error_code"] = exc.code
        return json.dumps(entry, default=str, separators=(",", ":"))


def configure_logging(settings: LoggingSettings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.level)
