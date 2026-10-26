"""Identity wired through the real composition root against real Postgres."""

from __future__ import annotations

from uuid import UUID

import pytest

from lda.bootstrap.container import build_platform
from lda.bootstrap.identity import IDENTITY_MIGRATIONS, build_identity
from lda.modules.identity.api import PrincipalKind, PrincipalReference, RegisterPrincipalCommand
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.testing.doubles import InMemoryAuditRecorder
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import NotFoundError
from lda.shared.kernel.ids import TenantId
from platform_support import provisioned_database


def test_identity_is_operational_through_bootstrap() -> None:
    with provisioned_database("lda_test_boot", "lda_app_boot", [IDENTITY_MIGRATIONS]) as db:
        config = ConfigurationLoader(
            {
                "LDA_DATABASE__URL": db.app_url,
                "LDA_OBJECT_STORAGE__BUCKET": "bucket",
                "LDA_JOBS__QUEUES": '{"events": 1}',
            }
        )
        platform = build_platform(config, audit_factory=lambda tx: InMemoryAuditRecorder())
        try:
            identity = build_identity(platform)
            a = SystemContext(TenantId(UUID(int=1)), "bootstrap-test")
            b = SystemContext(TenantId(UUID(int=2)), "bootstrap-test")
            view = identity.register_principal.execute(
                a, RegisterPrincipalCommand(PrincipalKind.SERVICE, "worker")
            )
            ref = PrincipalReference(view.principal_id)
            assert identity.disable_principal.execute(a, ref).is_active is False
            assert identity.enable_principal.execute(a, ref).is_active is True
            assert identity.get_principal.execute(a, ref).kind is PrincipalKind.SERVICE
            with pytest.raises(NotFoundError):
                identity.get_principal.execute(b, ref)
        finally:
            platform.dispose()
