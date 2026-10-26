"""Audit wiring: supplies the platform's `audit_factory` (ADR-019).

Audit has no dependencies and no use cases: every other module reaches it only through the shared
`AuditRecorder` port on its unit of work. Call `build_audit()` BEFORE `build_platform`, which
requires the factory and has no default (audit is never silently absent).
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.audit.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.platform.database.unit_of_work import AuditRecorderFactory

AUDIT_MIGRATIONS = MIGRATIONS_VERSIONS
"""Pass as `extra_locations` to `platform.database.migrate.upgrade` (creates the schema)."""


@dataclass(frozen=True)
class AuditContainer:
    recorder_factory: AuditRecorderFactory


def build_audit() -> AuditContainer:
    """Each unit of work gets `SqlAuditRecorder(tx)`: the audit row joins that transaction."""
    return AuditContainer(recorder_factory=SqlAuditRecorder)
