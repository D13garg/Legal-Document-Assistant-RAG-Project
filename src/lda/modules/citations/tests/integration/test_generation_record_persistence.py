"""Real Postgres: the GenerationRecord contract, forced RLS, insert-only enforcement, the
all-or-nothing citation set, rollback, concurrency and content-free errors (ADR-016)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.modules.citations.api import (
    RECORD_EXISTS,
    Citation,
    CitationSource,
    GenerationOutcome,
    GenerationRecord,
    GenerationRecordRepository,
)
from lda.modules.citations.api import CitedDocumentId as DocumentId
from lda.modules.citations.api import CitedVersionId as VersionId
from lda.modules.citations.infrastructure.persistence.repositories import (
    CORRUPT,
    FAILED,
    TENANT_MISMATCH,
    SqlGenerationRecordRepository,
)
from lda.modules.citations.infrastructure.persistence.unit_of_work import (
    UnitOfWorkGenerationRecords,
)
from lda.modules.citations.testing import InMemoryGenerationRecords
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId

T1, T2 = TenantId(UUID(int=0xC1)), TenantId(UUID(int=0xC2))
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
H = "a" * 64


def source(n: int, pages: tuple[int, int] | None = None) -> CitationSource:
    return CitationSource(
        DocumentId(UUID(int=0x100 + n)),
        VersionId(UUID(int=0x200 + n)),
        UUID(int=0x300 + n),
        n,
        f"{n:064x}",
        *(pages or (None, None)),
    )


def make(
    tenant: TenantId = T1,
    outcome: GenerationOutcome = GenerationOutcome.ANSWERED,
    record_id: UUID | None = None,
    cited: tuple[Citation, ...] | None = None,
) -> GenerationRecord:
    if cited is None:
        cited = (
            (Citation("S2", source(2, (3, 4))), Citation("S1", source(1)))
            if outcome is GenerationOutcome.ANSWERED
            else ()
        )
    return GenerationRecord(
        record_id=record_id or uuid4(),
        tenant_id=tenant,
        principal_id=PrincipalId(uuid4()),
        request_id=RequestId(uuid4()),
        created_at=NOW,
        config_id="rag-" + H,
        outcome=outcome,
        template_id="grounded_answer",
        template_version="v1",
        model_id="fake-model:1",
        request_sha256="b" * 64,
        evidence_fingerprint="c" * 64,
        citations=cited,
    )


@pytest.fixture(params=["memory", "sql"])
def repo(
    request: pytest.FixtureRequest, sql_records: UnitOfWorkGenerationRecords
) -> GenerationRecordRepository:
    return InMemoryGenerationRecords() if request.param == "memory" else sql_records


# ---- the contract, against both implementations -------------------------------------------


@pytest.mark.parametrize("outcome", list(GenerationOutcome))
def test_round_trip_preserves_every_field_and_citation_order(
    repo: GenerationRecordRepository, outcome: GenerationOutcome
) -> None:
    record = make(outcome=outcome)
    repo.add(record)
    assert repo.get(T1, record.record_id) == record


def test_a_duplicate_id_is_a_conflict_and_the_original_is_untouched(
    repo: GenerationRecordRepository,
) -> None:
    first = make()
    repo.add(first)
    with pytest.raises(ConflictError) as raised:
        repo.add(make(record_id=first.record_id, outcome=GenerationOutcome.REJECTED))
    assert raised.value.code == RECORD_EXISTS
    assert repo.get(T1, first.record_id) == first


def test_get_is_tenant_bound_and_unknown_ids_are_none(repo: GenerationRecordRepository) -> None:
    record = make()
    repo.add(record)
    assert repo.get(T2, record.record_id) is None
    assert repo.get(T1, uuid4()) is None


def test_the_same_record_id_may_exist_in_two_tenants(repo: GenerationRecordRepository) -> None:
    shared = uuid4()
    a, b = make(T1, record_id=shared), make(T2, record_id=shared)
    repo.add(a)
    repo.add(b)
    assert repo.get(T1, shared) == a and repo.get(T2, shared) == b


# ---- Postgres-only guarantees --------------------------------------------------------------


def test_the_schema_stores_no_content_columns(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        columns = {
            (r.table_name, r.column_name)
            for r in conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'citations'"
                )
            )
        }
    names = {c for _, c in columns}
    assert names.isdisjoint({"prompt", "answer", "question", "excerpt", "text", "proof", "content"})
    assert {t for t, _ in columns} == {"generation_records", "generation_record_citations"}


def test_both_tables_have_forced_rls_and_no_update_delete_grants(
    admin_engine: Engine, database: object
) -> None:
    with admin_engine.connect() as conn:
        flags = conn.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'citations' AND relkind = 'r'"
            )
        ).all()
        assert len(flags) == 2 and all(f.relrowsecurity and f.relforcerowsecurity for f in flags)
        for table in ("generation_records", "generation_record_citations"):
            granted = {
                p
                for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES")
                if conn.execute(
                    text("SELECT has_table_privilege('lda_app_citations', :t, :p)"),
                    {"t": f"citations.{table}", "p": p},
                ).scalar()
            }
            assert granted == {"SELECT", "INSERT"}


def test_rls_hides_and_blocks_other_tenants_and_a_missing_tenant_context(
    sql_records: UnitOfWorkGenerationRecords, app_engine: Engine
) -> None:
    record = make()
    sql_records.add(record)
    with app_engine.begin() as conn:  # no tenant GUC at all: fail closed
        assert conn.execute(text("SELECT count(*) FROM citations.generation_records")).scalar() == 0
    with TenantTransaction(app_engine, T2) as tx:
        seen = tx.session.execute(text("SELECT count(*) FROM citations.generation_records"))
        assert seen.scalar() == 0
        with pytest.raises(DBAPIError):  # WITH CHECK: T2 cannot write a row for T1
            tx.session.execute(
                text(
                    "INSERT INTO citations.generation_records VALUES (:t, :r, :p, :q, now(), "
                    "'rag-' || :h, 'rejected', 'x', 'v', 'm', :h, :h, 0)"
                ),
                {"t": T1.value, "r": uuid4(), "p": uuid4(), "q": uuid4(), "h": H},
            )


def test_the_repository_refuses_a_foreign_tenant_before_touching_the_database(
    app_engine: Engine,
) -> None:
    with TenantTransaction(app_engine, T1) as tx:
        bound = SqlGenerationRecordRepository(tx)
        with pytest.raises(InfrastructureError) as add_error:
            bound.add(make(T2))
        with pytest.raises(InfrastructureError) as get_error:
            bound.get(T2, uuid4())
    assert add_error.value.code == get_error.value.code == TENANT_MISMATCH


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE citations.generation_records SET model_id = 'other'",
        "DELETE FROM citations.generation_records",
        "UPDATE citations.generation_record_citations SET ordinal = 9",
        "DELETE FROM citations.generation_record_citations",
        "TRUNCATE citations.generation_records, citations.generation_record_citations",
    ],
)
def test_records_are_insert_only_for_the_runtime_role_and_even_the_owner(
    sql_records: UnitOfWorkGenerationRecords,
    app_engine: Engine,
    admin_engine: Engine,
    statement: str,
) -> None:
    record = make()
    sql_records.add(record)
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, T1) as tx:
        tx.session.execute(text(statement))
    with pytest.raises(DBAPIError) as owner, admin_engine.begin() as conn:
        conn.execute(text(statement))
    assert "insert-only" in str(owner.value.orig)
    assert sql_records.get(T1, record.record_id) == record


def test_a_record_with_missing_or_extra_citation_rows_cannot_commit(
    app_engine: Engine,
) -> None:
    good = make()
    head = (
        "INSERT INTO citations.generation_records VALUES (:t, :r, :p, :q, now(), 'rag-' || :h, "
        "'answered', 'x', 'v', 'm', :h, :h, 2)"
    )
    params = {"t": T1.value, "r": good.record_id, "p": uuid4(), "q": uuid4(), "h": H}
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, T1) as tx:
        tx.session.execute(text(head), params)
        tx.commit()
    with TenantTransaction(app_engine, T1) as tx:
        assert (
            tx.session.execute(text("SELECT count(*) FROM citations.generation_records")).scalar()
            == 0
        )


def test_citations_cannot_be_added_to_an_existing_record_later(
    sql_records: UnitOfWorkGenerationRecords, app_engine: Engine
) -> None:
    record = make()
    sql_records.add(record)
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, T1) as tx:
        tx.session.execute(
            text(
                "INSERT INTO citations.generation_record_citations VALUES "
                "(:t, :r, 9, 'S9', :d, :v, :c, 9, :h, NULL, NULL)"
            ),
            {
                "t": T1.value,
                "r": record.record_id,
                "d": uuid4(),
                "v": uuid4(),
                "c": uuid4(),
                "h": H,
            },
        )
        tx.commit()
    assert sql_records.get(T1, record.record_id) == record


def test_the_database_refuses_an_answered_record_without_citations(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, T1) as tx:
        tx.session.execute(
            text(
                "INSERT INTO citations.generation_records VALUES (:t, :r, :p, :q, now(), "
                "'rag-' || :h, 'answered', 'x', 'v', 'm', :h, :h, 0)"
            ),
            {"t": T1.value, "r": uuid4(), "p": uuid4(), "q": uuid4(), "h": H},
        )


def test_an_uncommitted_write_leaves_nothing_behind(
    sql_records: UnitOfWorkGenerationRecords, app_engine: Engine
) -> None:
    record = make()
    with TenantTransaction(app_engine, T1) as tx:
        SqlGenerationRecordRepository(tx).add(record)
        tx.rollback()
    assert sql_records.get(T1, record.record_id) is None
    with TenantTransaction(app_engine, T1) as tx:
        counts = tx.session.execute(
            text("SELECT count(*) FROM citations.generation_record_citations")
        )
        assert counts.scalar() == 0


def test_database_failures_surface_as_a_stable_content_free_code(
    sql_records: UnitOfWorkGenerationRecords, admin_engine: Engine
) -> None:
    record = make()
    sql_records.add(record)
    with admin_engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM citations.generation_record_citations WHERE marker = 'S1'"))
    with pytest.raises(InfrastructureError) as raised:  # stored set no longer matches its count
        sql_records.get(T1, record.record_id)
    assert raised.value.code == CORRUPT
    rendered = repr(raised.value) + str(raised.value)
    for secret in (str(record.record_id), record.config_id, "b" * 64, "c" * 64):
        assert secret not in rendered
    assert FAILED == "citations_persistence_failed"


def test_concurrent_submissions_of_one_id_yield_exactly_one_record(
    sql_records: UnitOfWorkGenerationRecords,
) -> None:
    shared = uuid4()
    outcomes: list[str] = []
    gate = threading.Barrier(4)

    def submit() -> None:
        gate.wait()
        try:
            sql_records.add(make(record_id=shared))
            outcomes.append("ok")
        except ConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=submit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["conflict"] * 3 + ["ok"]
    assert sql_records.get(T1, shared) is not None
