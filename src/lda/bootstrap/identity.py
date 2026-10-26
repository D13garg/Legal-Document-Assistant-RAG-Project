"""Identity wiring: the only place that joins Identity's use cases to the SQL adapters."""

from __future__ import annotations

from dataclasses import dataclass

from lda.bootstrap.container import PlatformContainer
from lda.modules.identity.api import (
    DisablePrincipal,
    EnablePrincipal,
    GetPrincipal,
    RegisterPrincipal,
)
from lda.modules.identity.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.identity.infrastructure.persistence.unit_of_work import (
    SqlIdentityUnitOfWorkFactory,
)

IDENTITY_MIGRATIONS = MIGRATIONS_VERSIONS
"""Pass as `extra_locations` to `platform.database.migrate.upgrade` (creates the schema)."""


@dataclass(frozen=True)
class IdentityContainer:
    register_principal: RegisterPrincipal
    disable_principal: DisablePrincipal
    enable_principal: EnablePrincipal
    get_principal: GetPrincipal


def build_identity(platform: PlatformContainer) -> IdentityContainer:
    units_of_work = SqlIdentityUnitOfWorkFactory(platform.units_of_work)
    return IdentityContainer(
        register_principal=RegisterPrincipal(units_of_work, platform.clock, platform.ids),
        disable_principal=DisablePrincipal(units_of_work, platform.clock),
        enable_principal=EnablePrincipal(units_of_work, platform.clock),
        get_principal=GetPrincipal(units_of_work),
    )
