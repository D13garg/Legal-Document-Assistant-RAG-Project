from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(schema="platform")

jobs = Table(
    "jobs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("queue", Text, nullable=False),
    Column("job_type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("run_at", DateTime(timezone=True), nullable=False),
    Column("unique_key", Text),
    Column("fencing_token", BigInteger, nullable=False, server_default=text("0")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
