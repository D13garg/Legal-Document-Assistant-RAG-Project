"""Programmatic Alembic runner. Each module owns a branch + schema; platform owns `platform`."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config

_ROLE = re.compile(r"^[a-z_][a-z0-9_]*$")
_HERE = Path(__file__).parent


def _config(admin_url: str, app_role: str, extra_locations: Sequence[str]) -> Config:
    if not _ROLE.match(app_role):
        raise ValueError("invalid role name")
    cfg = Config()
    cfg.set_main_option("script_location", str(_HERE / "migrations"))
    cfg.set_main_option("path_separator", "os")
    versions = str(_HERE / "migrations" / "versions")
    cfg.set_main_option("version_locations", os.pathsep.join([versions, *extra_locations]))
    cfg.attributes["admin_url"] = admin_url
    cfg.attributes["app_role"] = app_role
    return cfg


def upgrade(
    admin_url: str,
    *,
    app_role: str = "lda_app",
    extra_locations: Sequence[str] = (),
    target: str = "heads",
) -> None:
    """Run with the *owner/admin* credentials, never the runtime role."""
    command.upgrade(_config(admin_url, app_role, extra_locations), target)


def downgrade(
    admin_url: str,
    *,
    target: str,
    app_role: str = "lda_app",
    extra_locations: Sequence[str] = (),
) -> None:
    command.downgrade(_config(admin_url, app_role, extra_locations), target)
