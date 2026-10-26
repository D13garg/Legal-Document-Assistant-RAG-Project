from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKeyConstraint, Integer, MetaData, Table, Text, Uuid

metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("run_id", Uuid, primary_key=True),
    Column("document_id", Uuid, nullable=False),
    Column("version_id", Uuid, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("error_code", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("progress_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("acknowledged_at", DateTime(timezone=True), nullable=True),
    Column("redeliveries", Integer, nullable=False),
    schema="ingestion",
)

stage_runs = Table(
    "stage_runs",
    metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("run_id", Uuid, primary_key=True),
    Column("stage", Text, primary_key=True),
    Column("position", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("duration_ms", Integer, nullable=True),
    Column("error_code", Text, nullable=True),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"], ["ingestion.runs.tenant_id", "ingestion.runs.run_id"]
    ),
    schema="ingestion",
)
