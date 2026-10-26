from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, Integer, MetaData, Table, Text, Uuid

metadata = MetaData()

evaluation_runs = Table(
    "evaluation_runs",
    metadata,
    Column("sequence", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", Uuid, nullable=False, unique=True),
    Column("config_id", Text, nullable=False),
    Column("dataset_id", Text, nullable=False),
    Column("dataset_version", Integer, nullable=False),
    Column("dataset_fingerprint", Text, nullable=False),
    Column("evaluated_at", BigInteger, nullable=False),
    Column("passed", Boolean, nullable=False),
    Column("record_text", Text, nullable=False),
    Column("record_sha256", Text, nullable=False),
    schema="evaluation",
)

release_eligibility = Table(
    "release_eligibility",
    metadata,
    Column("sequence", BigInteger, primary_key=True, autoincrement=True),
    Column("eligibility_id", Uuid, nullable=False, unique=True),
    Column("run_id", Uuid, nullable=False, unique=True),
    Column("config_id", Text, nullable=False),
    Column("dataset_fingerprint", Text, nullable=False),
    Column("run_sha256", Text, nullable=False),
    Column("run_passed", Boolean, nullable=False),
    Column("recorded_at", BigInteger, nullable=False),
    schema="evaluation",
)
