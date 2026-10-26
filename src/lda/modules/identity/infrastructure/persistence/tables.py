from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text, Uuid

SCHEMA = "identity"

metadata = MetaData()

principals = Table(
    "principals",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("tenant_id", Uuid, nullable=False),
    Column("kind", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status_changed_at", DateTime(timezone=True), nullable=False),
    schema="identity",
)
