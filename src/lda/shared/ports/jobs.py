from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.ids import JobId, TenantId

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


@dataclass(frozen=True, kw_only=True)
class JobSpec:
    queue: str
    job_type: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    unique_key: str | None = None  # a duplicate enqueue with the same key is a no-op
    run_at: datetime | None = None
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if not self.queue or not self.job_type:
            raise ValueError("queue and job_type are required")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.unique_key is not None and not self.unique_key.strip():
            raise ValueError("unique_key must not be blank (it would suppress every other job)")
        if self.run_at is not None and self.run_at.utcoffset() is None:
            raise ValueError("run_at must be timezone-aware")


class JobQueue(Protocol):
    """Tenant-bound transactional enqueue: the job exists iff the transaction commits."""

    def enqueue(self, spec: JobSpec) -> JobId | None:
        """Return the new job id, or None when `unique_key` already exists (idempotent no-op)."""
        ...


@dataclass(frozen=True, kw_only=True)
class ClaimedJob:
    id: JobId
    tenant_id: TenantId
    queue: str
    job_type: str
    payload: Mapping[str, JsonValue]
    attempt: int
    max_attempts: int
    fencing_token: int  # unique per claim of this job and never decreasing (ADR-012 amendment)


@dataclass(frozen=True)
class Succeeded:
    pass


@dataclass(frozen=True, kw_only=True)
class RetryAfter:
    delay: timedelta
    error_code: str
    consumes_attempt: bool = True  # False: provider outage backoff must not burn attempts


@dataclass(frozen=True, kw_only=True)
class Failed:
    error_code: str  # permanent: the job goes to the dead-letter state


JobOutcome = Succeeded | RetryAfter | Failed


class JobHandler(Protocol):
    """Executes one job type. Must be idempotent: delivery is at-least-once."""

    def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome: ...


class JobFence(Protocol):
    """Lets an effect transaction prove that its job's lease is still current (ADR-012)."""

    def verify(self, job_id: JobId, fencing_token: int) -> None:
        """Raise `ConflictError("job_lease_lost")` unless the job is still `running` in this
        tenant under `fencing_token` with an unexpired lease. While the caller's transaction is
        open the lease cannot be reclaimed, so effects cannot commit after another worker took over.
        """
        ...
