"""Where Alembic finds the conversations migration branch (for `platform.database.migrate`)."""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_VERSIONS = str(Path(__file__).parent / "migrations" / "versions")
