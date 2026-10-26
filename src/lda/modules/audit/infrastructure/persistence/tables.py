from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, FetchedValue, MetaData, Table, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB

SCHEMA = "audit"

metadata = MetaData()

# `record_id` is database-generated (GENERATED ALWAYS AS IDENTITY). The adapter never supplies it
# and never reads it back: the runtime role holds INSERT only, and `RETURNING` would need SELECT.
records = Table(
    "records",
    metadata,
    Column(
        "record_id",
        BigInteger,
        primary_key=True,
        autoincrement=False,
        server_default=FetchedValue(),
    ),
    Column("tenant_id", Uuid, nullable=False),
    Column("actor", Uuid, nullable=True),
    Column("action", Text, nullable=False),
    Column("resource_type", Text, nullable=False),
    Column("resource_id", Text, nullable=True),
    Column("outcome", Text, nullable=False),
    Column("request_id", Uuid, nullable=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("attributes", JSONB, nullable=False),
    schema="audit",
    implicit_returning=False,
)
