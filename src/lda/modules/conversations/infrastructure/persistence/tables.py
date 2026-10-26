from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text, Uuid

SCHEMA = "conversations"

metadata = MetaData()

conversations = Table(
    "conversations",
    metadata,
    Column("tenant_id", Uuid, nullable=False, primary_key=True),
    Column("id", Uuid, nullable=False, primary_key=True),
    Column("owner_id", Uuid, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("message_count", Integer, nullable=False),
    schema=SCHEMA,
)

messages = Table(
    "messages",
    metadata,
    Column("tenant_id", Uuid, nullable=False, primary_key=True),
    Column("conversation_id", Uuid, nullable=False, primary_key=True),
    Column("sequence", Integer, nullable=False, primary_key=True),
    Column("role", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("client_message_id", Uuid, nullable=True),
    Column("text", Text, nullable=True),
    Column("record_id", Uuid, nullable=True),
    schema=SCHEMA,
)

deletion_receipts = Table(
    "deletion_receipts",
    metadata,
    Column("tenant_id", Uuid, nullable=False, primary_key=True),
    Column("conversation_id", Uuid, nullable=False, primary_key=True),
    Column("deleted_at", DateTime(timezone=True), nullable=False),
    Column("messages_deleted", Integer, nullable=False),
    Column("scope", Text, nullable=False),
    schema=SCHEMA,
)
