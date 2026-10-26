from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

index_versions = Table(
    "index_versions",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("version_id", Uuid, primary_key=True),
    Column("index_id", Text, primary_key=True),
    Column("document_id", Uuid, nullable=False),
    Column("embedding_provider", Text, nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("embedding_version", Text, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("chunker_version", Text, nullable=False),
    Column("source_hash", Text, nullable=False),
    Column("chunk_count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema="retrieval",
)

# The `embedding public.vector(256)` column is deliberately not mapped: vectors are written and
# searched only through the two explicit statements in repositories.py (numbers in, numbers out).
chunk_vectors = Table(
    "chunk_vectors",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("index_id", Text, primary_key=True),
    Column("chunk_id", Uuid, primary_key=True),
    Column("document_id", Uuid, nullable=False),
    Column("version_id", Uuid, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("text_hash", Text, nullable=False),
    schema="retrieval",
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
    schema="retrieval",
)
