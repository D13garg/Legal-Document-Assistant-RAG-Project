"""The in-memory recorder: staged until commit, discarded on rollback, tenant-bound."""

from __future__ import annotations

import pytest

from lda.modules.audit.domain.errors import InvalidAuditEntry
from lda.modules.audit.testing import InMemoryAuditStore, StagedAuditRecorder
from lda.modules.audit.tests.conftest import TENANT_A, TENANT_B, a_fact


def test_nothing_is_visible_before_commit() -> None:
    store = InMemoryAuditStore()
    StagedAuditRecorder(TENANT_A, store).record(a_fact())
    assert store.entries == []


def test_a_rollback_discards_staged_facts_even_if_commit_follows() -> None:
    store = InMemoryAuditStore()
    recorder = StagedAuditRecorder(TENANT_A, store)
    recorder.record(a_fact())
    recorder.rollback()
    recorder.commit()
    assert store.entries == []


def test_a_commit_publishes_once_and_only_once() -> None:
    store = InMemoryAuditStore()
    recorder = StagedAuditRecorder(TENANT_A, store)
    recorder.record(a_fact())
    recorder.commit()
    recorder.commit()
    assert len(store.entries) == 1


def test_for_tenant_filters_by_tenant() -> None:
    store = InMemoryAuditStore()
    for tenant in (TENANT_A, TENANT_B):
        recorder = StagedAuditRecorder(tenant, store)
        recorder.record(a_fact(tenant_id=tenant))
        recorder.commit()
    assert [e.tenant_id for e in store.for_tenant(TENANT_B)] == [TENANT_B]


def test_a_foreign_tenant_fact_is_rejected() -> None:
    recorder = StagedAuditRecorder(TENANT_A, InMemoryAuditStore())
    with pytest.raises(InvalidAuditEntry):
        recorder.record(a_fact(tenant_id=TENANT_B))
