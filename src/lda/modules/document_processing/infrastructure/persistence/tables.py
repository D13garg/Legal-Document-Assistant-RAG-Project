from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

extractions = Table(
    "extractions",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("version_id", Uuid, primary_key=True),
    Column("document_id", Uuid, nullable=False),
    Column("parser_version", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("artifact_bytes", BigInteger, nullable=False),
    Column("block_count", Integer, nullable=False),
    Column("character_count", BigInteger, nullable=False),
    Column("page_count", Integer, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema="document_processing",
)

purge_receipts = Table(
    "purge_receipts",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("document_id", Uuid, primary_key=True),
    Column("purged_at", DateTime(timezone=True), nullable=False),
    Column("versions_purged", Integer, nullable=False),
    Column("version_ids", JSONB, nullable=False),
    Column("scope", Text, nullable=False),
    schema="document_processing",
)
