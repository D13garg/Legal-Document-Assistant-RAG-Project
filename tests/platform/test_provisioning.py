"""ADR-044 provisioning. Embedded PostgreSQL over a Unix socket with trust authentication:
this verifies the SQL, the role/RLS checks and the migration assembly. It does NOT verify password
authentication, Docker Compose or a standalone server.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import lda.bootstrap as bootstrap_package
from lda.bootstrap.provisioning import RUNTIME_MIGRATIONS, RUNTIME_SCHEMAS, run_command
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.database.provisioning import (
    ProvisioningError,
    ProvisioningPlan,
    check_runtime_connection,
    make_plan,
    provision,
    verify_schema_security,
)
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.ports.jobs import JobSpec
from platform_support import _server_url, enqueue_job

PASSWORD = "runtime-pw-1"  # noqa: S105 - throwaway test credential


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    generator = _server_url()
    yield next(generator)
    next(generator, None)


@dataclass(frozen=True)
class Env:
    database: str
    role: str
    admin_url: str
    runtime_url: str


def fresh(server: str, password: str = PASSWORD) -> Env:
    suffix = uuid4().hex[:10]
    database, role = f"prov_{suffix}", f"lda_app_{suffix}"
    root = create_engine(make_url(server), isolation_level="AUTOCOMMIT")
    with root.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {database}"))
    root.dispose()
    admin = make_url(server).set(database=database)
    runtime = admin.set(username=role, password=password)
    return Env(
        database,
        role,
        admin.render_as_string(hide_password=False),
        runtime.render_as_string(hide_password=False),
    )


def admin_sql(env: Env, sql: str, **params: object) -> list[tuple[object, ...]]:
    engine = create_engine(env.admin_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return [tuple(r) for r in result.all()] if result.returns_rows else []
    finally:
        engine.dispose()


def run(env: Env) -> object:
    return provision(
        env.admin_url, env.runtime_url, locations=RUNTIME_MIGRATIONS, schemas=RUNTIME_SCHEMAS
    )


# ---- fresh database and repeat ---------------------------------------------------------------


def test_fresh_provisioning_then_repeat_is_safe(server: str) -> None:
    env = fresh(server)
    first = run(env)
    assert first.role_created is True  # type: ignore[attr-defined]
    versions = admin_sql(env, "SELECT version_num FROM alembic_version ORDER BY 1")
    assert len(versions) >= len(RUNTIME_MIGRATIONS)

    second = run(env)
    assert second.role_created is False  # type: ignore[attr-defined]
    assert admin_sql(env, "SELECT version_num FROM alembic_version ORDER BY 1") == versions


def test_the_runtime_role_is_least_privilege_and_owns_nothing(server: str) -> None:
    env = fresh(server)
    run(env)
    flags = admin_sql(
        env,
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, rolcanlogin, "
        "rolinherit FROM pg_roles WHERE rolname = :r",
        r=env.role,
    )
    assert flags == [(False, False, False, False, False, True, False)]
    owned = admin_sql(
        env,
        "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
        "WHERE r.rolname = :r",
        r=env.role,
    )
    assert owned == [(0,)]
    assert admin_sql(
        env, "SELECT rolpassword IS NOT NULL FROM pg_authid WHERE rolname = :r", r=env.role
    ) == [(True,)]


def test_a_password_with_quotes_colons_and_percent_is_stored_safely(server: str) -> None:
    env = fresh(server, password="p%s:a'b\"c\\d;--")  # noqa: S106
    run(env)
    assert admin_sql(
        env, "SELECT rolpassword IS NOT NULL FROM pg_authid WHERE rolname = :r", r=env.role
    ) == [(True,)]


def test_the_command_prints_no_credential_or_url(
    server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    env = fresh(server)
    config = ConfigurationLoader(
        {
            "LDA_PROVISIONING__ADMIN_URL": env.admin_url,
            "LDA_DATABASE__URL": env.runtime_url,
        }
    )
    assert run_command(config) == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert PASSWORD not in output and "postgresql" not in output and env.database not in output
    assert "runtime role created" in output


# ---- refusals: nothing is migrated -----------------------------------------------------------


@pytest.mark.parametrize(
    ("attributes", "code"),
    [
        ("SUPERUSER LOGIN", "runtime_role_is_superuser"),
        ("BYPASSRLS LOGIN", "runtime_role_bypasses_rls"),
        ("CREATEDB LOGIN", "runtime_role_has_elevated_attributes"),
        ("CREATEROLE LOGIN", "runtime_role_has_elevated_attributes"),
        ("REPLICATION LOGIN", "runtime_role_has_elevated_attributes"),
        ("NOLOGIN", "runtime_role_cannot_login"),
    ],
)
def test_an_unsafe_existing_role_is_refused_before_any_migration(
    server: str, attributes: str, code: str
) -> None:
    env = fresh(server)
    admin_sql(env, f"CREATE ROLE {env.role} {attributes}")
    with pytest.raises(ProvisioningError) as caught:
        run(env)
    assert caught.value.code == code
    assert admin_sql(env, "SELECT count(*) FROM pg_namespace WHERE nspname = 'platform'") == [(0,)]
    assert admin_sql(env, "SELECT to_regclass('alembic_version') IS NULL") == [(True,)]


def test_an_existing_role_is_never_altered(server: str) -> None:
    env = fresh(server)
    admin_sql(env, f"CREATE ROLE {env.role} LOGIN CREATEDB")
    with pytest.raises(ProvisioningError):
        run(env)
    assert admin_sql(env, "SELECT rolcreatedb FROM pg_roles WHERE rolname = :r", r=env.role) == [
        (True,)
    ]


def test_a_role_with_memberships_is_refused(server: str) -> None:
    env = fresh(server)
    group = f"grp_{uuid4().hex[:8]}"
    admin_sql(env, f"CREATE ROLE {group}")
    admin_sql(env, f"CREATE ROLE {env.role} LOGIN")
    admin_sql(env, f"GRANT {group} TO {env.role}")
    with pytest.raises(ProvisioningError) as caught:
        run(env)
    assert caught.value.code == "runtime_role_has_memberships"


def test_a_role_that_owns_objects_is_refused(server: str) -> None:
    env = fresh(server)
    admin_sql(env, f"CREATE ROLE {env.role} LOGIN")
    admin_sql(env, "CREATE TABLE public.owned_by_runtime (x int)")
    admin_sql(env, f"ALTER TABLE public.owned_by_runtime OWNER TO {env.role}")
    with pytest.raises(ProvisioningError) as caught:
        run(env)
    assert caught.value.code == "runtime_role_owns_objects"


def test_unmanaged_schema_state_is_refused_not_repaired(server: str) -> None:
    env = fresh(server)
    admin_sql(env, "CREATE SCHEMA identity")
    with pytest.raises(ProvisioningError) as caught:
        run(env)
    assert caught.value.code == "unmanaged_schema_present"
    assert admin_sql(env, "SELECT to_regclass('alembic_version') IS NULL") == [(True,)]
    assert admin_sql(env, "SELECT count(*) FROM pg_namespace WHERE nspname = 'identity'") == [(1,)]


@pytest.mark.parametrize(
    ("admin", "runtime", "code"),
    [
        (
            "postgresql+psycopg://same:a@h/db",
            "postgresql+psycopg://same:b@h/db",
            "admin_and_runtime_role_must_differ",
        ),
        (
            "postgresql+psycopg://adm:a@h/db1",
            "postgresql+psycopg://app:b@h/db2",
            "admin_and_runtime_database_differ",
        ),
        (
            "postgresql+psycopg://adm:a@h1/db",
            "postgresql+psycopg://app:b@h2/db",
            "admin_and_runtime_database_differ",
        ),
        (
            "postgresql+psycopg://adm:a@h/db",
            "postgresql+psycopg://app@h/db",
            "runtime_password_missing",
        ),
        (
            "postgresql+psycopg://adm:a@h/db",
            "postgresql+psycopg://App-Role:b@h/db",
            "runtime_role_name_invalid",
        ),
        ("not a url", "postgresql+psycopg://app:b@h/db", "database_url_invalid"),
    ],
)
def test_the_url_pair_is_validated_without_connecting(admin: str, runtime: str, code: str) -> None:
    with pytest.raises(ProvisioningError) as caught:
        make_plan(admin, runtime)
    assert caught.value.code == code
    assert "a@" not in repr(caught.value) and ":b@" not in repr(caught.value)


def test_missing_configuration_is_refused_before_any_connection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = ConfigurationLoader({"LDA_DATABASE__URL": "postgresql+psycopg://app:pw@localhost/db"})
    assert run_command(config) == 2
    assert "refused" in capsys.readouterr().err
    config = ConfigurationLoader(
        {"LDA_PROVISIONING__ADMIN_URL": "postgresql+psycopg://a:b@localhost/db"}
    )
    assert run_command(config) == 2


def test_the_command_exits_1_with_a_code_only_on_failure(
    server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    env = fresh(server)
    admin_sql(env, f"CREATE ROLE {env.role} LOGIN SUPERUSER")
    config = ConfigurationLoader(
        {"LDA_PROVISIONING__ADMIN_URL": env.admin_url, "LDA_DATABASE__URL": env.runtime_url}
    )
    assert run_command(config) == 1
    err = capsys.readouterr().err
    assert "runtime_role_is_superuser" in err and PASSWORD not in err


def test_a_migration_failure_reports_a_code_and_never_the_driver_message(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = fresh(server)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("could not connect password=topsecret host=db.internal")

    monkeypatch.setattr("lda.platform.database.migrate.upgrade", boom)
    with pytest.raises(ProvisioningError) as caught:
        run(env)
    assert caught.value.code == "migration_failed"
    assert "topsecret" not in repr(caught.value) + caught.value.detail
    assert caught.value.__cause__ is None


# ---- post-migration verification -------------------------------------------------------------


def test_a_tenant_table_without_forced_rls_fails_verification(server: str) -> None:
    env = fresh(server)
    run(env)
    admin_sql(env, "CREATE TABLE identity.rogue (tenant_id uuid)")
    plan = make_plan(env.admin_url, env.runtime_url)
    with pytest.raises(ProvisioningError) as caught:
        verify_schema_security(plan, RUNTIME_SCHEMAS)
    assert caught.value.code == "tenant_table_without_forced_rls"
    assert "identity.rogue" in caught.value.detail


def test_enabled_but_not_forced_rls_still_fails_verification(server: str) -> None:
    env = fresh(server)
    run(env)
    admin_sql(env, "CREATE TABLE identity.half (tenant_id uuid)")
    admin_sql(env, "ALTER TABLE identity.half ENABLE ROW LEVEL SECURITY")
    with pytest.raises(ProvisioningError) as caught:
        verify_schema_security(make_plan(env.admin_url, env.runtime_url), RUNTIME_SCHEMAS)
    assert "identity.half" in caught.value.detail


def test_a_runtime_role_with_create_on_a_schema_fails_verification(server: str) -> None:
    env = fresh(server)
    run(env)
    admin_sql(env, f"GRANT CREATE ON SCHEMA identity TO {env.role}")
    with pytest.raises(ProvisioningError) as caught:
        verify_schema_security(make_plan(env.admin_url, env.runtime_url), RUNTIME_SCHEMAS)
    assert caught.value.code == "runtime_role_can_create_in_schema"


def test_a_missing_expected_schema_fails_verification(server: str) -> None:
    env = fresh(server)
    run(env)
    with pytest.raises(ProvisioningError) as caught:
        verify_schema_security(
            make_plan(env.admin_url, env.runtime_url), (*RUNTIME_SCHEMAS, "never_created")
        )
    assert caught.value.code == "expected_schema_missing"


def test_the_runtime_connection_check_rejects_a_privileged_connection(server: str) -> None:
    env = fresh(server)
    run(env)
    admin_user = make_url(env.admin_url).username or ""
    plan = ProvisioningPlan(admin_user, "unused", env.admin_url, env.admin_url)
    with pytest.raises(ProvisioningError) as caught:
        check_runtime_connection(plan)
    assert caught.value.code == "runtime_connection_not_least_privilege"


def test_rls_hides_existing_jobs_from_the_runtime_role_without_a_tenant(server: str) -> None:
    env = fresh(server)
    run(env)
    engine = create_engine(env.runtime_url)
    try:
        enqueue_job(
            engine,
            FakeClock(),
            SequentialIdGenerator(),
            1,
            JobSpec(queue="events", job_type="probe"),
        )
    finally:
        engine.dispose()
    assert admin_sql(env, "SELECT count(*) FROM platform.jobs") == [(1,)]  # the row exists...
    check_runtime_connection(make_plan(env.admin_url, env.runtime_url))  # ...and RLS hides it


# ---- migration assembly ----------------------------------------------------------------------


def test_runtime_migrations_cover_every_bootstrap_migration_constant() -> None:
    declared: set[str] = set()
    for info in pkgutil.iter_modules(bootstrap_package.__path__):
        module = importlib.import_module(f"lda.bootstrap.{info.name}")
        for name in dir(module):
            if name.endswith("_MIGRATIONS") and name != "RUNTIME_MIGRATIONS":
                declared.add(getattr(module, name))
    assert declared - set(RUNTIME_MIGRATIONS) == set()
    assert len(set(RUNTIME_MIGRATIONS)) == len(RUNTIME_MIGRATIONS)
    assert all(Path(p).is_dir() and any(Path(p).glob("*.py")) for p in RUNTIME_MIGRATIONS)


def test_runtime_schemas_equal_what_the_migrations_really_create(server: str) -> None:
    env = fresh(server)
    run(env)
    created = {
        row[0]
        for row in admin_sql(
            env,
            "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
            "AND nspname NOT IN ('information_schema', 'public')",
        )
    }
    assert created == set(RUNTIME_SCHEMAS)


# ---- password handling (ADR-045 D4) ----------------------------------------------------------

SECRET = "Pl41n-Secret/w0rd+=%:'"  # noqa: S105 - throwaway test credential


def test_the_plaintext_password_never_reaches_the_server(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    sent: list[str] = []
    real_connection_execute = psycopg.Connection.execute
    real_cursor_execute = psycopg.Cursor.execute

    def spy_connection(self: object, query: object, params: object = None, **kw: object) -> object:
        sent.append(repr((query, params)))
        return real_connection_execute(self, query, params, **kw)  # type: ignore[call-overload]

    def spy_cursor(self: object, query: object, params: object = None, **kw: object) -> object:
        sent.append(repr((query, params)))
        return real_cursor_execute(self, query, params, **kw)  # type: ignore[call-overload]

    monkeypatch.setattr(psycopg.Connection, "execute", spy_connection)
    monkeypatch.setattr(psycopg.Cursor, "execute", spy_cursor)
    env = fresh(server, password=SECRET)
    run(env)
    monkeypatch.undo()

    create = [s for s in sent if s.startswith(("('CREATE ROLE", '("CREATE ROLE'))]
    assert len(set(create)) == 1 and "SCRAM-SHA-256$" in create[0]  # only a verifier was sent
    assert SECRET not in "".join(sent)
    stored = admin_sql(env, "SELECT rolpassword FROM pg_authid WHERE rolname = :r", r=env.role)
    assert str(stored[0][0]).startswith("SCRAM-SHA-256$4096:") and SECRET not in str(stored[0][0])


def test_the_stored_verifier_really_authenticates_over_scram(server: str) -> None:
    """Real SCRAM-SHA-256 authentication on the embedded server's Unix socket for this role."""
    env = fresh(server, password=SECRET)
    run(env)
    (hba,) = admin_sql(env, "SHOW hba_file")[0]
    path = Path(str(hba))
    original = path.read_text()
    path.write_text(f"local {env.database} {env.role} scram-sha-256\n" + original)
    try:
        admin_sql(env, "SELECT pg_reload_conf()")
        good = create_engine(env.runtime_url)
        try:
            with good.connect() as conn:
                assert conn.execute(text("SELECT current_user")).scalar_one() == env.role
        finally:
            good.dispose()
        bad = create_engine(make_url(env.runtime_url).set(password="wrong-password"))  # noqa: S106
        try:
            with pytest.raises(Exception, match="authentication failed"), bad.connect():
                pass
        finally:
            bad.dispose()
        check_runtime_connection(make_plan(env.admin_url, env.runtime_url))  # via SCRAM too
    finally:
        path.write_text(original)
        admin_sql(env, "SELECT pg_reload_conf()")
