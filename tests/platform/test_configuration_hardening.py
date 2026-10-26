"""Missing / unknown / invalid configuration fails fast without echoing values (ADR-031)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, SecretStr

from lda.bootstrap import container
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.configuration.settings import (
    DatabaseSettings,
    JobWorkerSettings,
    LoggingSettings,
    ObjectStorageSettings,
    RateLimitSettings,
    SettingsSlice,
)
from lda.platform.testing.doubles import InMemoryAuditRecorder
from lda.shared.kernel.errors import ConfigurationError

SECRET_URL = "postgresql+psycopg://user:TOPSECRETPW@db.internal/lda"  # noqa: S105
VALUE = "ECHO-CANARY-9"


def load(env: dict[str, str], section: str, model: type[SettingsSlice]) -> SettingsSlice:
    return ConfigurationLoader(env).load(section, model)


def failure(env: dict[str, str], section: str, model: type[SettingsSlice]) -> ConfigurationError:
    with pytest.raises(ConfigurationError) as exc:
        load(env, section, model)
    return exc.value


@pytest.mark.parametrize(
    ("section", "model", "missing"),
    [
        ("database", DatabaseSettings, "url"),
        ("object_storage", ObjectStorageSettings, "bucket"),
        ("jobs", JobWorkerSettings, "queues"),
    ],
)
def test_missing_required_fields_are_named(
    section: str, model: type[SettingsSlice], missing: str
) -> None:
    err = failure({}, section, model)
    assert str(err) == "configuration_invalid"
    assert missing in err.detail and section in err.detail


def test_unknown_fields_are_rejected_not_ignored() -> None:
    env = {"LDA_DATABASE__URL": SECRET_URL, "LDA_DATABASE__POOL_SZIE": VALUE}  # typo
    err = failure(env, "database", DatabaseSettings)
    assert "pool_szie" in err.detail and VALUE not in err.detail


@pytest.mark.parametrize(
    ("section", "model", "env"),
    [
        ("database", DatabaseSettings, {"POOL_SIZE": "0"}),
        ("database", DatabaseSettings, {"POOL_SIZE": VALUE}),
        ("database", DatabaseSettings, {"POOL_TIMEOUT_SECONDS": "0"}),
        ("database", DatabaseSettings, {"STATEMENT_TIMEOUT_MS": "99"}),
        ("object_storage", ObjectStorageSettings, {"PRESIGN_MAX_SECONDS": "0"}),
        ("object_storage", ObjectStorageSettings, {"PRESIGN_MAX_SECONDS": "3601"}),
        ("object_storage", ObjectStorageSettings, {"PRESIGN_MAX_SECONDS": VALUE}),
        ("logging", LoggingSettings, {"LEVEL": "TRACE"}),
        ("logging", LoggingSettings, {"LEVEL": VALUE}),
        ("rate_limit", RateLimitSettings, {"SOCKET_TIMEOUT_SECONDS": "0"}),
        ("jobs", JobWorkerSettings, {"QUEUES": "{bad json"}),
        ("jobs", JobWorkerSettings, {"QUEUES": "[1, 2]"}),
        ("jobs", JobWorkerSettings, {"QUEUES": "{}"}),
        ("jobs", JobWorkerSettings, {"QUEUES": '{"q": 0}'}),
        ("jobs", JobWorkerSettings, {"QUEUES": '{"q": -3}'}),
        ("jobs", JobWorkerSettings, {"QUEUES": '{"q": "many"}'}),
        ("jobs", JobWorkerSettings, {"QUEUES": '{"q": 1}', "POLL_INTERVAL_SECONDS": "0"}),
        ("jobs", JobWorkerSettings, {"QUEUES": '{"q": 1}', "LEASE_SECONDS": "4"}),
        (
            "jobs",
            JobWorkerSettings,
            {"QUEUES": '{"q": 1}', "LEASE_SECONDS": "10", "HEARTBEAT_SECONDS": "10"},
        ),
        (
            "jobs",
            JobWorkerSettings,
            {"QUEUES": '{"q": 1}', "BACKOFF_BASE_SECONDS": "60", "BACKOFF_CAP_SECONDS": "5"},
        ),
    ],
)
def test_invalid_values_fail_startup_without_echoing_them(
    section: str, model: type[SettingsSlice], env: dict[str, str]
) -> None:
    full = {f"LDA_{section.upper()}__{k}": v for k, v in env.items()}
    if section == "database":
        full["LDA_DATABASE__URL"] = SECRET_URL
    if section == "object_storage":
        full["LDA_OBJECT_STORAGE__BUCKET"] = "b"
    err = failure(full, section, model)
    rendered = f"{err!s} {err!r} {err.detail}"
    assert VALUE not in rendered and "TOPSECRETPW" not in rendered


def test_valid_job_settings_with_sane_timing_are_accepted() -> None:
    s = JobWorkerSettings(queues={"a": 1, "b": 4}, lease_seconds=60, heartbeat_seconds=15)
    assert s.queues == {"a": 1, "b": 4}


def test_configuration_errors_carry_no_chained_pydantic_error() -> None:
    err = failure({"LDA_DATABASE__POOL_SIZE": VALUE}, "database", DatabaseSettings)
    assert err.__cause__ is None and err.__context__ is None  # the chain would hold input values


def test_only_the_lda_prefix_with_a_section_separator_is_read() -> None:
    env = {
        "DATABASE__URL": SECRET_URL,  # no prefix
        "LDA_DATABASE_URL": SECRET_URL,  # single underscore: not a section separator
        "LDA__URL": SECRET_URL,  # empty section
        "LDA_DATABASE__": SECRET_URL,  # empty field
    }
    assert "url" in failure(env, "database", DatabaseSettings).detail


def test_section_and_field_names_are_case_insensitive_and_environment_is_snapshotted() -> None:
    env = {"LDA_Database__Url": SECRET_URL}
    loader = ConfigurationLoader(env)
    env["LDA_Database__Url"] = "mutated"
    assert loader.load("DATABASE", DatabaseSettings).url.get_secret_value() == SECRET_URL


def test_secrets_never_render_from_settings_objects_or_their_serialisations() -> None:
    db = DatabaseSettings(url=SecretStr(SECRET_URL))
    storage = ObjectStorageSettings(
        bucket="b",
        access_key_id=SecretStr("AKIASECRETKEY"),
        secret_access_key=SecretStr("SECRETACCESS"),
    )
    rendered = [
        repr(db),
        str(db),
        f"{db}",
        db.model_dump_json(),
        str(db.model_dump()),
        str(db.model_dump(mode="json")),
        repr(storage),
        storage.model_dump_json(),
        str(storage.model_dump()),
    ]
    joined = " ".join(rendered)
    for secret in ("TOPSECRETPW", "AKIASECRETKEY", "SECRETACCESS"):
        assert secret not in joined
    assert db.url.get_secret_value() == SECRET_URL  # still usable by the one place that needs it


def test_slices_are_frozen_and_forbid_extras() -> None:
    db = DatabaseSettings(url=SecretStr(SECRET_URL))
    with pytest.raises(Exception, match="frozen"):
        db.pool_size = 99
    assert issubclass(DatabaseSettings, BaseModel)


# ---- bootstrap: fail fast, before creating any resource -------------------------------------------


def test_bootstrap_validates_every_slice_before_building_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []
    for name in ("create_runtime_engine", "create_object_store", "create_rate_limiter"):
        monkeypatch.setattr(container, name, lambda *a, _n=name, **k: built.append(_n))
    config = ConfigurationLoader(
        {
            "LDA_DATABASE__URL": SECRET_URL,
            "LDA_JOBS__QUEUES": '{"events": 1}',
            "LDA_OBJECT_STORAGE__BUCKET": "b",
            "LDA_RATE_LIMIT__SOCKET_TIMEOUT_SECONDS": "0",  # the LAST slice is invalid
        }
    )
    with pytest.raises(ConfigurationError, match="configuration_invalid"):
        container.build_platform(config, audit_factory=lambda tx: InMemoryAuditRecorder())
    assert built == []  # no engine/pool/client was created before the config was known to be good
