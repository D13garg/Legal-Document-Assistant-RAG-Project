"""One behavioural contract, run against the in-memory recorder and the Postgres recorder.

Both go through the same domain validation, bind to one tenant, and make facts visible only on
commit.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from lda.modules.audit.domain.errors import InvalidAuditEntry
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.audit.testing import InMemoryAuditStore, StagedAuditRecorder
from lda.modules.audit.tests.conftest import (
    ACTOR,
    REQUEST,
    TENANT_A,
    TENANT_B,
    WHEN,
    a_fact,
)
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.audit import AuditOutcome, AuditRecorder


@dataclass(frozen=True)
class Stored:
    tenant_id: TenantId
    actor: UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    request_id: UUID | None
    occurred_at: datetime
    attributes: dict[str, str]


class Session:
    """One unit of work's recorder plus how to finish it."""

    def __init__(
        self, recorder: AuditRecorder, commit: Callable[[], None], rollback: Callable[[], None]
    ):
        self.recorder = recorder
        self.commit = commit
        self.rollback = rollback


class Backend:
    def __init__(
        self, open_session: Callable[[TenantId], Session], committed: Callable[[], list[Stored]]
    ):
        self.open = open_session
        self.committed = committed


@pytest.fixture(params=["memory", "sql"])
def backend(request: pytest.FixtureRequest) -> Iterator[Backend]:
    if request.param == "memory":
        store = InMemoryAuditStore()

        def open_memory(tenant: TenantId) -> Session:
            rec = StagedAuditRecorder(tenant, store)
            return Session(rec, rec.commit, rec.rollback)

        def memory_rows() -> list[Stored]:
            return [
                Stored(
                    e.tenant_id,
                    e.actor.value if e.actor else None,
                    e.action,
                    e.resource_type,
                    e.resource_id,
                    e.outcome,
                    e.request_id.value if e.request_id else None,
                    e.occurred_at,
                    dict(e.attributes),
                )
                for e in store.entries
            ]

        yield Backend(open_memory, memory_rows)
        return

    app_engine: Engine = request.getfixturevalue("app_engine")
    admin_engine: Engine = request.getfixturevalue("admin_engine")
    open_txs: list[TenantTransaction] = []

    def open_sql(tenant: TenantId) -> Session:
        tx = TenantTransaction(app_engine, tenant).__enter__()
        open_txs.append(tx)
        return Session(SqlAuditRecorder(tx), tx.commit, tx.rollback)

    def sql_rows() -> list[Stored]:
        with admin_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tenant_id, actor, action, resource_type, resource_id, outcome, "
                    "request_id, occurred_at, attributes FROM audit.records ORDER BY record_id"
                )
            ).all()
        return [Stored(TenantId(r[0]), *r[1:]) for r in rows]

    yield Backend(open_sql, sql_rows)
    for tx in open_txs:
        tx.__exit__(None, None, None)  # anything left open is rolled back


def test_a_committed_fact_round_trips_exactly(backend: Backend) -> None:
    session = backend.open(TENANT_A)
    session.recorder.record(a_fact())
    assert backend.committed() == []  # not visible before commit
    session.commit()
    (row,) = backend.committed()
    assert row == Stored(
        TENANT_A,
        ACTOR.value,
        "identity.principal_disabled",
        "principal",
        "0f6c1a52-0000-4000-8000-000000000001",
        "success",
        REQUEST.value,
        WHEN,
        {"principal_kind": "user"},
    )


def test_a_rolled_back_unit_leaves_no_record(backend: Backend) -> None:
    session = backend.open(TENANT_A)
    session.recorder.record(a_fact())
    session.rollback()
    assert backend.committed() == []


def test_nullable_fields_round_trip_as_none(backend: Backend) -> None:
    session = backend.open(TENANT_A)
    session.recorder.record(a_fact(actor=None, request_id=None, resource_id=None, attributes={}))
    session.commit()
    (row,) = backend.committed()
    assert (row.actor, row.request_id, row.resource_id, row.attributes) == (None, None, None, {})


def test_system_purpose_is_an_ordinary_attribute(backend: Backend) -> None:
    session = backend.open(TENANT_A)
    session.recorder.record(a_fact(actor=None, attributes={"purpose": "dead-letter-requeue"}))
    session.commit()
    assert backend.committed()[0].attributes == {"purpose": "dead-letter-requeue"}


def test_denied_and_failure_outcomes_are_storable_but_nothing_emits_them_yet(
    backend: Backend,
) -> None:
    session = backend.open(TENANT_A)
    for outcome in (AuditOutcome.DENIED, AuditOutcome.FAILURE):
        session.recorder.record(a_fact(outcome=outcome))
    session.commit()
    assert [r.outcome for r in backend.committed()] == ["denied", "failure"]


def test_facts_keep_their_order(backend: Backend) -> None:
    session = backend.open(TENANT_A)
    for n in range(5):
        session.recorder.record(a_fact(resource_id=f"r{n}"))
    session.commit()
    assert [r.resource_id for r in backend.committed()] == [f"r{n}" for n in range(5)]


def test_a_fact_for_another_tenant_is_rejected_and_stores_nothing(backend: Backend) -> None:
    session = backend.open(TENANT_A)
    with pytest.raises(InvalidAuditEntry) as caught:
        session.recorder.record(a_fact(tenant_id=TENANT_B))
    assert caught.value.code == "audit_tenant_mismatch"
    session.commit()
    assert backend.committed() == []


INVALID: dict[str, dict[str, object]] = {
    "bad action": {"action": "Not An Action"},
    "bad resource type": {"resource_type": "Bad Type"},
    "bad resource id": {"resource_id": "has space"},
    "naive time": {"occurred_at": datetime(2026, 1, 1)},
    "too many attributes": {"attributes": {f"k{i}": "v" for i in range(17)}},
    "content key": {"attributes": {"document_text": "x"}},
    "multiline value": {"attributes": {"k": "a\nb"}},
}


@pytest.mark.parametrize("name", list(INVALID))
def test_an_invalid_fact_fails_the_action_and_stores_nothing(backend: Backend, name: str) -> None:
    session = backend.open(TENANT_A)
    session.recorder.record(a_fact(resource_id="kept"))  # an earlier, valid fact of the same unit
    with pytest.raises(InvalidAuditEntry):
        session.recorder.record(a_fact(**INVALID[name]))
    session.rollback()  # the caller's action fails, so its unit of work is rolled back
    assert backend.committed() == []


def test_each_tenant_only_ever_records_for_itself(backend: Backend) -> None:
    a, b = backend.open(TENANT_A), backend.open(TENANT_B)
    a.recorder.record(a_fact(tenant_id=TENANT_A, resource_id="a"))
    b.recorder.record(a_fact(tenant_id=TENANT_B, resource_id="b"))
    a.commit()
    b.commit()
    assert {(r.tenant_id, r.resource_id) for r in backend.committed()} == {
        (TENANT_A, "a"),
        (TENANT_B, "b"),
    }
