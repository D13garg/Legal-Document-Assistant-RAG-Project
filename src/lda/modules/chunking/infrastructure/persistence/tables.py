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

chunk_sets = Table(
    "chunk_sets",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("version_id", Uuid, primary_key=True),
    Column("document_id", Uuid, nullable=False),
    Column("extraction_hash", Text, nullable=False),
    Column("chunker_version", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("artifact_bytes", BigInteger, nullable=False),
    Column("chunk_count", Integer, nullable=False),
    Column("character_count", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema="chunking",
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
    schema="chunking",
)
