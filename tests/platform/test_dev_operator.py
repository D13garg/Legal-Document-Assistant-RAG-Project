"""ADR-043: the development-only operator bootstrap, against real Postgres and the runtime role."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from lda.bootstrap.access_control import ACCESS_CONTROL_MIGRATIONS
from lda.bootstrap.audit import AUDIT_MIGRATIONS
from lda.bootstrap.dev_operator import (
    PURPOSE,
    DevOperator,
    build_dev_operator,
    load_dev_operator_settings,
    run_command,
)
from lda.bootstrap.identity import IDENTITY_MIGRATIONS
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError
from platform_support import Database, provisioned_database

KEY = base64.urlsafe_b64encode(b"dev-operator-test-signing-key-32b!!").decode().rstrip("=")
T1, T2 = UUID(int=0xD0010001), UUID(int=0xD0010002)
DOC = UUID(int=0xD0C00001)


def env(db: Database, tenant: UUID = T1, **extra: str) -> dict[str, str]:
    return {
        "LDA_DATABASE__URL": db.app_url,
        "LDA_OBJECT_STORAGE__BUCKET": "bucket",
        "LDA_JOBS__QUEUES": '{"events": 1}',
        "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
        "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
        "LDA_DEV_OPERATOR__ENVIRONMENT": "development",
        "LDA_DEV_OPERATOR__TENANT_ID": str(tenant),
        **extra,
    }


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_devop",
        "lda_app_devop",
        [IDENTITY_MIGRATIONS, ACCESS_CONTROL_MIGRATIONS, AUDIT_MIGRATIONS],
    ) as db:
        yield db


def operator(environ: dict[str, str]) -> tuple[DevOperator, object]:
    return build_dev_operator(ConfigurationLoader(environ))


def admin_scalar(db: Database, sql: str, **params: object) -> object:
    engine = create_engine(db.admin_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


# ---- environment guards: no database needed -------------------------------------------------

LOCAL = "postgresql+psycopg://app:secret@localhost:5432/lda"


def guard_env(**override: str) -> dict[str, str]:
    base = {
        "LDA_DATABASE__URL": LOCAL,
        "LDA_DEV_OPERATOR__ENVIRONMENT": "development",
        "LDA_DEV_OPERATOR__TENANT_ID": str(T1),
    }
    base.update(override)
    return {k: v for k, v in base.items() if v != "<unset>"}


def test_guard_accepts_only_explicit_development_on_a_local_database() -> None:
    assert load_dev_operator_settings(ConfigurationLoader(guard_env())).tenant_id == T1


@pytest.mark.parametrize("value", ["<unset>", "production", "test", "", "Development", "dev"])
def test_guard_fails_closed_for_absent_or_any_other_environment(value: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_dev_operator_settings(
            ConfigurationLoader(guard_env(LDA_DEV_OPERATOR__ENVIRONMENT=value))
        )
    assert caught.value.code == "configuration_invalid"
    assert "secret" not in str(caught.value.detail)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://app:secret@db.prod.example.com:5432/lda",
        "postgresql+psycopg://app:secret@10.0.0.5/lda",
        "postgresql+psycopg://app:secret@/lda",  # no host and no socket path
    ],
)
def test_guard_refuses_a_non_loopback_database_without_echoing_credentials(url: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_dev_operator_settings(ConfigurationLoader(guard_env(LDA_DATABASE__URL=url)))
    assert caught.value.code == "dev_operator_requires_local_database"
    assert "secret" not in repr(caught.value) and "secret" not in caught.value.detail


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://app:pw@127.0.0.1/lda",
        "postgresql+psycopg://app:pw@[::1]/lda",
        "postgresql+psycopg://app:pw@/lda?host=/var/run/postgresql",
    ],
)
def test_guard_accepts_loopback_and_unix_socket(url: str) -> None:
    load_dev_operator_settings(ConfigurationLoader(guard_env(LDA_DATABASE__URL=url)))


def test_guard_rejects_unknown_keys_and_a_missing_tenant() -> None:
    with pytest.raises(ConfigurationError):
        load_dev_operator_settings(
            ConfigurationLoader(guard_env(LDA_DEV_OPERATOR__TENANT_ID="<unset>"))
        )
    with pytest.raises(ConfigurationError):
        load_dev_operator_settings(ConfigurationLoader(guard_env(LDA_DEV_OPERATOR__SUPERUSER="1")))


def test_build_fails_before_creating_a_pool_when_the_guard_fails() -> None:
    with pytest.raises(ConfigurationError):
        build_dev_operator(
            ConfigurationLoader(guard_env(LDA_DEV_OPERATOR__ENVIRONMENT="production"))
        )


# ---- real Postgres, runtime role -------------------------------------------------------------


def test_the_runtime_role_is_not_a_superuser_owner_or_bypassrls(database: Database) -> None:
    role = "lda_app_devop"
    flags = admin_scalar(
        database,
        "SELECT rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb FROM pg_roles "
        "WHERE rolname = :r",
        r=role,
    )
    assert flags is False
    owned = admin_scalar(
        database,
        "SELECT count(*) FROM pg_tables WHERE tableowner = :r AND schemaname IN "
        "('identity', 'access_control', 'audit')",
        r=role,
    )
    assert owned == 0
    # The operator talks to the database as that role (never the admin URL).
    assert make_url(database.app_url).username == role
    assert make_url(database.admin_url).username != role


def test_registers_a_principal_then_verifies_it_without_writing(database: Database) -> None:
    op, dispose = operator(env(database))
    try:
        first = op.ensure_principal()
    finally:
        dispose()  # type: ignore[operator]
    assert first.created and first.tenant_id.value == T1

    op2, dispose2 = operator(env(database, LDA_DEV_OPERATOR__PRINCIPAL_ID=str(first.principal_id)))
    try:
        again = op2.ensure_principal()
    finally:
        dispose2()  # type: ignore[operator]
    assert not again.created and again.principal_id == first.principal_id
    count = admin_scalar(
        database,
        "SELECT count(*) FROM identity.principals WHERE id = :i",
        i=first.principal_id.value,
    )
    assert count == 1


def test_registration_is_audited_as_system_work_with_no_actor_and_no_name(
    database: Database,
) -> None:
    op, dispose = operator(env(database, LDA_DEV_OPERATOR__DISPLAY_NAME="Zed Secret-Name"))
    try:
        outcome = op.ensure_principal()
    finally:
        dispose()  # type: ignore[operator]
    row = (
        create_engine(database.admin_url)
        .connect()
        .execute(
            text(
                "SELECT actor, attributes::text FROM audit.records "
                "WHERE action = 'identity.principal_registered' AND resource_id = :r"
            ),
            {"r": str(outcome.principal_id)},
        )
        .one()
    )
    assert row[0] is None
    assert PURPOSE in row[1] and "Zed Secret-Name" not in row[1]


def test_a_principal_of_another_tenant_is_refused(database: Database) -> None:
    op, dispose = operator(env(database))
    try:
        mine = op.ensure_principal()
    finally:
        dispose()  # type: ignore[operator]
    other, dispose2 = operator(
        env(database, tenant=T2, LDA_DEV_OPERATOR__PRINCIPAL_ID=str(mine.principal_id))
    )
    try:
        with pytest.raises(ConfigurationError) as caught:
            other.ensure_principal()
        assert caught.value.code == "dev_operator_principal_not_in_tenant"
        with pytest.raises(ConfigurationError):
            other.tenant_context()
        with pytest.raises(ConfigurationError):
            other.grant_first_read(DOC)
    finally:
        dispose2()  # type: ignore[operator]


def test_an_unknown_or_disabled_principal_is_refused(database: Database) -> None:
    ghost, dispose = operator(env(database, LDA_DEV_OPERATOR__PRINCIPAL_ID=str(uuid4())))
    try:
        with pytest.raises(ConfigurationError) as caught:
            ghost.ensure_principal()
        assert caught.value.code == "dev_operator_principal_not_in_tenant"
    finally:
        dispose()  # type: ignore[operator]

    op, dispose2 = operator(env(database))
    try:
        victim = op.ensure_principal()
    finally:
        dispose2()  # type: ignore[operator]
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:  # test setup only: put the row in the disabled state
        conn.execute(
            text(
                "UPDATE identity.principals SET status = 'disabled', version = version + 1, "
                "status_changed_at = now() WHERE id = :i"
            ),
            {"i": victim.principal_id.value},
        )
    engine.dispose()
    off, dispose3 = operator(env(database, LDA_DEV_OPERATOR__PRINCIPAL_ID=str(victim.principal_id)))
    try:
        with pytest.raises(ConfigurationError) as caught:
            off.tenant_context()
        assert caught.value.code == "dev_operator_principal_not_active"
    finally:
        dispose3()  # type: ignore[operator]


def test_the_first_grant_is_exactly_one_document_read_in_one_tenant(database: Database) -> None:
    op, dispose = operator(env(database))
    try:
        who = op.ensure_principal()
    finally:
        dispose()  # type: ignore[operator]
    op, dispose = operator(env(database, LDA_DEV_OPERATOR__PRINCIPAL_ID=str(who.principal_id)))
    try:
        ctx = op.tenant_context()
        assert ctx.tenant_id.value == T1 and ctx.principal_id == who.principal_id
        first = op.grant_first_read(DOC)
        repeat = op.grant_first_read(DOC)
    finally:
        dispose()  # type: ignore[operator]
    assert first.changed and not repeat.changed and first.policy_epoch == repeat.policy_epoch

    rows = (
        create_engine(database.admin_url)
        .connect()
        .execute(
            text(
                "SELECT tenant_id, resource_kind, resource_id, action FROM access_control.grants "
                "WHERE principal_id = :p"
            ),
            {"p": who.principal_id.value},
        )
        .all()
    )
    assert [tuple(r) for r in rows] == [(T1, "document", str(DOC), "documents.read_document")]

    audit = admin_scalar(
        database,
        "SELECT count(*) FROM audit.records WHERE action = 'access_control.access_granted' "
        "AND actor IS NULL AND resource_id = :d AND attributes::text LIKE :p",
        d=str(DOC),
        p=f"%{PURPOSE}%",
    )
    assert audit == 1  # the idempotent repeat is not audited (ADR-019 D5)


def test_the_grant_is_invisible_to_another_tenant_through_the_runtime_role(
    database: Database,
) -> None:
    runtime = create_engine(database.app_url)
    try:
        for tenant, expected in ((T1, True), (T2, False)):
            with runtime.begin() as conn:
                conn.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)}
                )
                n = conn.execute(
                    text("SELECT count(*) FROM access_control.grants WHERE resource_id = :d"),
                    {"d": str(DOC)},
                ).scalar_one()
            assert (n > 0) is expected
    finally:
        runtime.dispose()


# ---- the one-shot command --------------------------------------------------------------------


def test_command_refuses_outside_development_with_a_nonzero_status(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run_command(
        ConfigurationLoader(env(database, LDA_DEV_OPERATOR__ENVIRONMENT="production"))
    )
    err = capsys.readouterr().err
    assert status == 2 and "refused" in err
    assert "lda_app_devop" not in err and "pw" not in err.split("refused")[0]


def test_command_registers_and_prints_only_the_principal_id(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run_command(
        ConfigurationLoader(env(database, LDA_DEV_OPERATOR__DISPLAY_NAME="Hidden Name"))
    )
    out = capsys.readouterr().out
    assert (
        status == 0 and "principal registered:" in out and "LDA_DEV_OPERATOR__PRINCIPAL_ID=" in out
    )
    assert "Hidden Name" not in out and database.app_url not in out


def test_the_module_command_exits_2_outside_development_in_a_real_subprocess() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    environ = {
        **os.environ,
        **guard_env(LDA_DEV_OPERATOR__ENVIRONMENT="production"),
        "PYTHONPATH": os.pathsep.join([str(root), str(root / "src")]),
    }
    done = subprocess.run(
        [sys.executable, "-m", "apps.dev_bootstrap"],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 2
    assert "refused" in done.stderr and "secret" not in done.stderr + done.stdout
