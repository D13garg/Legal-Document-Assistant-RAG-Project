"""Where Alembic finds document_processing's migration branch (`platform.database.migrate`)."""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_VERSIONS = str(Path(__file__).parent / "migrations" / "versions")
