"""Non-destructive database provisioning (ADR-044; SCRAM verifier, never plaintext: ADR-045).

Generic: locations and schemas are parameters.

Four separate steps, none of which drops, resets, truncates or silently repairs anything:

1. `ensure_runtime_role`   admin connection: create the runtime role if absent, else only VALIDATE.
2. `run_migrations`        admin connection: the existing Alembic runner, nothing changed.
3. `verify_schema_security` admin connection: forced RLS on every tenant table, least privilege.
4. `check_runtime_connection` RUNTIME connection: it really is the unprivileged role.

Errors carry a stable code only. Connection strings and passwords never appear in a message,
because the underlying driver errors can contain them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from lda.platform.database import migrate
from lda.shared.kernel.errors import InfrastructureError

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class ProvisioningError(InfrastructureError):
    """Provisioning refused or failed. `code` is stable; `detail` never holds a secret."""


@dataclass(frozen=True, slots=True)
class ProvisioningPlan:
    role: str
    password: str  # the runtime password, taken from the runtime URL; never printed
    admin_url: str
    runtime_url: str


@dataclass(frozen=True, slots=True)
class ProvisioningReport:
    role_created: bool
    schemas: tuple[str, ...]
    runtime_role: str


def _endpoint(url: URL) -> tuple[str, int | None, str | None, str]:
    socket_dir = url.query.get("host")
    host = url.host or (str(socket_dir) if socket_dir else "")
    return (host.lower(), url.port, url.database, "socket" if socket_dir else "tcp")


def make_plan(admin_url: str, runtime_url: str) -> ProvisioningPlan:
    """Validate the pair of URLs. Nothing connects here."""
    try:
        admin, runtime = make_url(admin_url), make_url(runtime_url)
    except Exception:
        raise ProvisioningError("database_url_invalid") from None
    role = runtime.username or ""
    if not _IDENT.match(role):
        raise ProvisioningError("runtime_role_name_invalid")
    if not runtime.password:
        raise ProvisioningError("runtime_password_missing")
    if not admin.username or admin.username == role:
        raise ProvisioningError("admin_and_runtime_role_must_differ")
    if _endpoint(admin) != _endpoint(runtime):
        raise ProvisioningError("admin_and_runtime_database_differ")
    return ProvisioningPlan(role, runtime.password, admin_url, runtime_url)


def _engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=False)


def _validate_role(conn: Connection, role: str) -> str | None:
    """Return a refusal code for an existing role, or None when it is acceptable."""
    execute = conn.execute
    row = execute(
        text(
            "SELECT oid, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
            "rolbypassrls FROM pg_roles WHERE rolname = :r"
        ),
        {"r": role},
    ).one()
    oid, can_login, superuser, create_role, create_db, replication, bypass = row
    if superuser:
        return "runtime_role_is_superuser"
    if bypass:
        return "runtime_role_bypasses_rls"
    if create_role or create_db or replication:
        return "runtime_role_has_elevated_attributes"
    if not can_login:
        return "runtime_role_cannot_login"
    if execute(
        text("SELECT count(*) FROM pg_auth_members WHERE member = :o"), {"o": oid}
    ).scalar_one():
        return "runtime_role_has_memberships"
    owned = execute(
        text(
            "SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            " WHERE c.relowner = :o AND n.nspname NOT IN ('pg_catalog', 'information_schema', "
            " 'pg_toast')) + (SELECT count(*) FROM pg_namespace WHERE nspowner = :o "
            " AND nspname NOT LIKE 'pg\\_%') + (SELECT count(*) FROM pg_database "
            " WHERE datdba = :o)"
        ),
        {"o": oid},
    ).scalar_one()
    if owned:
        return "runtime_role_owns_objects"
    return None


def ensure_runtime_role(plan: ProvisioningPlan) -> bool:
    """Create the role if absent (True) or validate it (False). Never ALTERs an existing role."""
    engine = _engine(plan.admin_url)
    try:
        with engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": plan.role}
            ).first()
            if exists:
                problem = _validate_role(conn, plan.role)
                if problem:
                    raise ProvisioningError(problem)
                return False
            driver = conn.connection.driver_connection
            # The plaintext password never reaches the server: libpq derives a SCRAM-SHA-256
            # verifier on the client (PQencryptPasswordConn) and only that appears in the
            # statement text, so statement logs hold a verifier, not the password. There is no
            # plaintext fallback: failure to derive a verifier fails the run.
            try:
                verifier = driver.pgconn.encrypt_password(  # type: ignore[union-attr]
                    plan.password.encode(), plan.role.encode(), b"scram-sha-256"
                ).decode()
            except Exception:
                raise ProvisioningError("password_hashing_failed") from None
            if not verifier.startswith("SCRAM-SHA-256$"):
                raise ProvisioningError("password_hashing_failed")
            statement = conn.execute(
                text(
                    "SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L', "
                    "CAST(:r AS text), CAST(:v AS text))"
                ),
                {"r": plan.role, "v": verifier},
            ).scalar_one()
            driver.execute(statement)  # type: ignore[union-attr]
            return True
    except ProvisioningError:
        raise
    except Exception:
        raise ProvisioningError("role_provisioning_failed") from None
    finally:
        engine.dispose()


def _refuse_unmanaged_state(plan: ProvisioningPlan, schemas: Sequence[str]) -> None:
    engine = _engine(plan.admin_url)
    try:
        with engine.connect() as conn:
            managed = conn.execute(text("SELECT to_regclass('alembic_version')")).scalar_one()
            if managed is None:
                present = conn.execute(
                    text("SELECT count(*) FROM pg_namespace WHERE nspname = ANY(:s)"),
                    {"s": list(schemas)},
                ).scalar_one()
                if present:
                    raise ProvisioningError("unmanaged_schema_present")
    except ProvisioningError:
        raise
    except Exception:
        raise ProvisioningError("database_unreachable") from None
    finally:
        engine.dispose()


def run_migrations(
    plan: ProvisioningPlan, locations: Sequence[str], schemas: Sequence[str]
) -> None:
    _refuse_unmanaged_state(plan, schemas)
    try:
        migrate.upgrade(plan.admin_url, app_role=plan.role, extra_locations=list(locations))
    except Exception:
        raise ProvisioningError("migration_failed") from None


def verify_schema_security(plan: ProvisioningPlan, schemas: Sequence[str]) -> None:
    engine = _engine(plan.admin_url)
    try:
        with engine.connect() as conn:
            found = (
                conn.execute(
                    text("SELECT nspname FROM pg_namespace WHERE nspname = ANY(:s)"),
                    {"s": list(schemas)},
                )
                .scalars()
                .all()
            )
            if set(found) != set(schemas):
                raise ProvisioningError("expected_schema_missing")
            unprotected = (
                conn.execute(
                    text(
                        "SELECT n.nspname || '.' || c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = ANY(:s) AND c.relkind IN ('r', 'p') "
                        "AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
                        "            AND a.attname = 'tenant_id' AND NOT a.attisdropped) "
                        "AND NOT (c.relrowsecurity AND c.relforcerowsecurity) ORDER BY 1"
                    ),
                    {"s": list(schemas)},
                )
                .scalars()
                .all()
            )
            if unprotected:
                raise ProvisioningError(
                    "tenant_table_without_forced_rls", detail=", ".join(unprotected)
                )
            for schema in schemas:
                usage, create = conn.execute(
                    text(
                        "SELECT has_schema_privilege(:r, :s, 'USAGE'), "
                        "has_schema_privilege(:r, :s, 'CREATE')"
                    ),
                    {"r": plan.role, "s": schema},
                ).one()
                if not usage:
                    raise ProvisioningError("runtime_role_lacks_schema_usage", detail=schema)
                if create:
                    raise ProvisioningError("runtime_role_can_create_in_schema", detail=schema)
            problem = _validate_role(conn, plan.role)
            if problem:
                raise ProvisioningError(problem)
    except ProvisioningError:
        raise
    except Exception:
        raise ProvisioningError("schema_verification_failed") from None
    finally:
        engine.dispose()


def check_runtime_connection(plan: ProvisioningPlan) -> None:
    """Connect as the RUNTIME role: proves the role works and is not privileged."""
    engine = _engine(plan.runtime_url)
    try:
        with engine.connect() as conn:
            user, privileged = conn.execute(
                text(
                    "SELECT current_user, r.rolsuper OR r.rolbypassrls "
                    "FROM pg_roles r WHERE r.rolname = current_user"
                )
            ).one()
            if user != plan.role or privileged:
                raise ProvisioningError("runtime_connection_not_least_privilege")
            visible = conn.execute(text("SELECT count(*) FROM platform.jobs")).scalar_one()
            if visible:  # no tenant context was set: RLS must show nothing
                raise ProvisioningError("runtime_role_sees_rows_without_tenant")
    except ProvisioningError:
        raise
    except Exception:
        raise ProvisioningError("runtime_connection_failed") from None
    finally:
        engine.dispose()


def provision(
    admin_url: str,
    runtime_url: str,
    *,
    locations: Sequence[str],
    schemas: Sequence[str],
) -> ProvisioningReport:
    plan = make_plan(admin_url, runtime_url)
    created = ensure_runtime_role(plan)
    run_migrations(plan, locations, schemas)
    verify_schema_security(plan, schemas)
    check_runtime_connection(plan)
    return ProvisioningReport(created, tuple(schemas), plan.role)
