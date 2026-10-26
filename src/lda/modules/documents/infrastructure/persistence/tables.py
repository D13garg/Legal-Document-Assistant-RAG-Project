from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, Table, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

documents = Table(
    "documents",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("document_id", Uuid, primary_key=True),
    Column("title", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_by", Uuid, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("latest_version_number", Integer, nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    schema="documents",
)

versions = Table(
    "versions",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("version_id", Uuid, primary_key=True),
    Column("document_id", Uuid, nullable=False),
    Column("number", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("created_by", Uuid, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema="documents",
)

upload_intents = Table(
    "upload_intents",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("version_id", Uuid, primary_key=True),
    Column("document_id", Uuid, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema="documents",
)

purge_receipts = Table(
    "purge_receipts",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("document_id", Uuid, primary_key=True),
    Column("tombstoned_at", DateTime(timezone=True), nullable=False),
    Column("purged_at", DateTime(timezone=True), nullable=False),
    Column("versions_purged", Integer, nullable=False),
    Column("version_ids", JSONB, nullable=False),
    Column("scope", Text, nullable=False),
    schema="documents",
)
