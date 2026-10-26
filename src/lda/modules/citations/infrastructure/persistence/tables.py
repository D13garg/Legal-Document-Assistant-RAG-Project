from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text, Uuid

metadata = MetaData()

generation_records = Table(
    "generation_records",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("record_id", Uuid, primary_key=True),
    Column("principal_id", Uuid, nullable=False),
    Column("request_id", Uuid, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("config_id", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("template_id", Text, nullable=False),
    Column("template_version", Text, nullable=False),
    Column("model_id", Text, nullable=False),
    Column("request_sha256", Text, nullable=False),
    Column("evidence_fingerprint", Text, nullable=False),
    Column("citation_count", Integer, nullable=False),
    schema="citations",
)

generation_record_citations = Table(
    "generation_record_citations",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("record_id", Uuid, primary_key=True),
    Column("marker", Text, primary_key=True),
    Column("position", Integer, nullable=False),
    Column("document_id", Uuid, nullable=False),
    Column("version_id", Uuid, nullable=False),
    Column("chunk_id", Uuid, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("text_hash", Text, nullable=False),
    Column("page_start", Integer, nullable=True),
    Column("page_end", Integer, nullable=True),
    schema="citations",
)
