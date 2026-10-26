from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, Uuid

SCHEMA = "access_control"

metadata = MetaData()

grants = Table(
    "grants",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("principal_id", Uuid, primary_key=True),
    Column("resource_kind", Text, primary_key=True),
    Column("resource_id", Text, primary_key=True),
    Column("action", Text, primary_key=True),
    Column("granted_at", DateTime(timezone=True), nullable=False),
    schema="access_control",
)

policy_epochs = Table(
    "policy_epochs",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("epoch", BigInteger, nullable=False),
    schema="access_control",
)
