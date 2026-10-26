"""Provisioning composition (ADR-044): the explicit migration set and the command body.

`apps/provision.py` is a thin shell around `run_command`. The generic, database-touching steps
live in `lda.platform.database.provisioning`; this module only assembles module constants.
Queue validation is NOT here: it stays inside the builders the worker calls (Step 18).
"""

from __future__ import annotations

import sys

from pydantic import SecretStr

from lda.bootstrap.access_control import ACCESS_CONTROL_MIGRATIONS
from lda.bootstrap.audit import AUDIT_MIGRATIONS
from lda.bootstrap.chunking import CHUNKING_MIGRATIONS
from lda.bootstrap.citations import CITATIONS_MIGRATIONS
from lda.bootstrap.conversations import CONVERSATIONS_MIGRATIONS
from lda.bootstrap.documents import DOCUMENTS_MIGRATIONS
from lda.bootstrap.identity import IDENTITY_MIGRATIONS
from lda.bootstrap.ingestion import INGESTION_MIGRATIONS
from lda.bootstrap.processing import PROCESSING_MIGRATIONS
from lda.bootstrap.retrieval import RETRIEVAL_MIGRATIONS
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.configuration.settings import DatabaseSettings, SettingsSlice
from lda.platform.database.provisioning import ProvisioningError, provision
from lda.shared.kernel.errors import LdaError

# Independent Alembic branches (no cross-branch dependency), so the order is not significant. This
# is the set the answering end-to-end test provisions. Evaluation is excluded on purpose: nothing
# in the worker or the headless slice uses it.
RUNTIME_MIGRATIONS: tuple[str, ...] = (
    IDENTITY_MIGRATIONS,
    ACCESS_CONTROL_MIGRATIONS,
    AUDIT_MIGRATIONS,
    DOCUMENTS_MIGRATIONS,
    INGESTION_MIGRATIONS,
    PROCESSING_MIGRATIONS,
    CHUNKING_MIGRATIONS,
    RETRIEVAL_MIGRATIONS,
    CITATIONS_MIGRATIONS,
    CONVERSATIONS_MIGRATIONS,
)

# One schema per migrated branch, plus the platform's. A test asserts this equals what the
# migrations really create, so drift fails loudly.
RUNTIME_SCHEMAS: tuple[str, ...] = (
    "platform",
    "identity",
    "access_control",
    "audit",
    "documents",
    "ingestion",
    "document_processing",
    "chunking",
    "retrieval",
    "citations",
    "conversations",
)


class ProvisioningSettings(SettingsSlice):
    """`LDA_PROVISIONING__ADMIN_URL`: the owner/admin URL. Read by this command only."""

    admin_url: SecretStr


def run_command(config: ConfigurationLoader) -> int:
    """Exit status: 0 provisioned, 1 failed, 2 refused before any connection."""
    try:
        admin = config.load("provisioning", ProvisioningSettings)
        runtime = config.load("database", DatabaseSettings)
    except LdaError as exc:
        print(f"provision: refused: {exc.code} {exc.detail}".rstrip(), file=sys.stderr)
        return 2
    try:
        report = provision(
            admin.admin_url.get_secret_value(),
            runtime.url.get_secret_value(),
            locations=RUNTIME_MIGRATIONS,
            schemas=RUNTIME_SCHEMAS,
        )
    except ProvisioningError as exc:
        print(f"provision: failed: {exc.code} {exc.detail}".rstrip(), file=sys.stderr)
        return 1
    print(
        f"runtime role {'created' if report.role_created else 'validated'}: {report.runtime_role}"
    )
    print(f"migrations applied: {len(RUNTIME_MIGRATIONS)} module branches + platform")
    print(f"verified: forced RLS on tenant tables in {len(report.schemas)} schemas")
    print("verified: runtime connection is unprivileged")
    return 0
