"""DEVELOPMENT-ONLY trusted-operator bootstrap (ADR-043). NOT authentication.

Trust is exactly: whoever controls the process environment of a command they run themselves.
This module is the only place that turns operator configuration into a tenant, a principal and
the first `documents.read_document` grant, and it does so through the real, unmodified
`RegisterPrincipal`, `GetPrincipal` and `GrantAccess` under a `SystemContext` (audited with a
purpose and no actor, ADR-019 D6). It adds no authorization exception and changes no module.

It must never be imported by the worker, a request path or a module (import-linter forbids the
last). Remove or replace it when an authenticated API layer defines tenant identity.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from lda.bootstrap.access_control import (
    AccessControlContainer,
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.audit import build_audit
from lda.bootstrap.container import build_platform
from lda.bootstrap.documents import register_doc_resources
from lda.bootstrap.identity import IdentityContainer, build_identity
from lda.modules.access_control.api import (
    ActionId,
    GrantAccessCommand,
    PolicyChangeView,
    ResourceRef,
    ResourceRegistry,
)
from lda.modules.documents.api import DOCUMENT_KIND, READ_DOCUMENT
from lda.modules.identity.api import (
    PrincipalKind,
    PrincipalReference,
    PrincipalStatus,
    RegisterPrincipalCommand,
)
from lda.platform.clock import RandomIdGenerator
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.configuration.settings import DatabaseSettings
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.errors import ConfigurationError, LdaError, NotFoundError
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId

SECTION = "dev_operator"
PURPOSE = "dev_operator_bootstrap"  # recorded in audit as the `purpose` attribute
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class DevOperatorSettings(BaseModel):
    """`LDA_DEV_OPERATOR__*`. `environment` has no default: absent or any other value fails."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Literal["development"]
    tenant_id: UUID
    principal_id: UUID | None = None
    display_name: str = Field(default="Development Operator", min_length=1, max_length=200)


def is_local_database(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.hostname
    if host is None:  # `?host=/path` is a Unix socket; anything else without a host is refused
        return any(h.startswith("/") for h in parse_qs(parts.query).get("host", []))
    return host.lower() in _LOOPBACK


def load_dev_operator_settings(config: ConfigurationLoader) -> DevOperatorSettings:
    """Fail closed BEFORE any connection pool exists. Errors never echo values."""
    settings = config.load(SECTION, DevOperatorSettings)
    database = config.load("database", DatabaseSettings)
    if not is_local_database(database.url.get_secret_value()):
        raise ConfigurationError(
            "dev_operator_requires_local_database",
            detail="the development operator refuses a non-loopback database host",
        )
    return settings


@dataclass(frozen=True, slots=True)
class BootstrapOutcome:
    tenant_id: TenantId
    principal_id: PrincipalId
    created: bool  # False: the configured principal was verified and nothing was written


class DevOperator:
    """Construct only from validated `DevOperatorSettings` (see `load_dev_operator_settings`)."""

    def __init__(
        self,
        settings: DevOperatorSettings,
        identity: IdentityContainer,
        access: AccessControlContainer,
    ) -> None:
        self._tenant = TenantId(settings.tenant_id)
        self._configured = (
            PrincipalId(settings.principal_id) if settings.principal_id is not None else None
        )
        self._display_name = settings.display_name
        self._identity = identity
        self._access = access
        self._ids = RandomIdGenerator()
        self._system = SystemContext(self._tenant, PURPOSE)

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant

    def ensure_principal(self) -> BootstrapOutcome:
        """Verify the configured principal, or register one (its id is not a secret)."""
        if self._configured is None:
            view = self._identity.register_principal.execute(
                self._system, RegisterPrincipalCommand(PrincipalKind.USER, self._display_name)
            )
            return BootstrapOutcome(self._tenant, view.principal_id, created=True)
        return BootstrapOutcome(self._tenant, self._verified_principal(), created=False)

    def _verified_principal(self) -> PrincipalId:
        if self._configured is None:
            raise ConfigurationError("dev_operator_principal_not_configured")
        try:  # RLS: another tenant's principal id looks absent, same as a wrong id
            view = self._identity.get_principal.execute(
                self._system, PrincipalReference(self._configured)
            )
        except NotFoundError:
            raise ConfigurationError("dev_operator_principal_not_in_tenant") from None
        if view.tenant_id != self._tenant:
            raise ConfigurationError("dev_operator_principal_not_in_tenant")
        if view.kind is not PrincipalKind.USER:
            raise ConfigurationError("dev_operator_principal_not_a_user")
        if view.status is not PrincipalStatus.ACTIVE:
            raise ConfigurationError("dev_operator_principal_not_active")
        return view.principal_id

    def tenant_context(self) -> TenantContext:
        """The only trusted user context in the slice; built after the principal is verified."""
        principal = self._verified_principal()
        return TenantContext(self._tenant, principal, RequestId(self._ids.new_uuid()))

    def grant_first_read(self, resource_id: UUID) -> PolicyChangeView:
        """The first grant: READ of ONE document, to the configured principal, in its tenant.

        There is deliberately no parameter for action, kind, principal or tenant.
        """
        principal = self._verified_principal()
        return self._access.grant_access.execute(
            self._system,
            GrantAccessCommand(
                principal,
                ResourceRef(DOCUMENT_KIND, str(resource_id)),
                ActionId(READ_DOCUMENT),
            ),
        )


def build_dev_operator(config: ConfigurationLoader) -> tuple[DevOperator, Callable[[], None]]:
    """Minimal stack for the one-shot command: audit, platform, identity, access control.

    Every slice is validated before the pool is created. Returns the operator and `dispose`.
    """
    settings = load_dev_operator_settings(config)
    ac_settings = load_access_control_settings(config)
    platform = build_platform(config, audit_factory=build_audit().recorder_factory)
    try:
        identity = build_identity(platform)
        registry = ResourceRegistry()
        register_doc_resources(registry)
        access = build_access_control(platform, identity, ac_settings, registry)
        return DevOperator(settings, identity, access), platform.dispose
    except BaseException:
        platform.dispose()
        raise


def run_command(config: ConfigurationLoader) -> int:
    """The one-shot command body (`apps/dev_bootstrap.py` is a thin shell around it).

    Exit status: 0 success, 1 failure after start-up, 2 refused at start-up. Prints the principal
    id only: no credential, display name or document content.
    """
    try:
        operator, dispose = build_dev_operator(config)
    except LdaError as exc:
        print(f"dev_bootstrap: refused: {exc.code} {exc.detail}".rstrip(), file=sys.stderr)
        return 2
    try:
        outcome = operator.ensure_principal()
    except LdaError as exc:
        print(f"dev_bootstrap: failed: {exc.code}", file=sys.stderr)
        return 1
    finally:
        dispose()
    verb = "registered" if outcome.created else "verified"
    print(f"principal {verb}: {outcome.principal_id}")
    if outcome.created:
        print(f"set LDA_DEV_OPERATOR__PRINCIPAL_ID={outcome.principal_id} to reuse it")
    return 0
