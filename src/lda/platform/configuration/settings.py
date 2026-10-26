"""Platform-owned settings slices. Modules define their own slices in `<module>/config.py`."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator


class SettingsSlice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DatabaseSettings(SettingsSlice):
    url: SecretStr  # runtime role: subject to RLS, not the table owner
    pool_size: int = Field(default=10, ge=1)
    pool_timeout_seconds: float = Field(default=10.0, gt=0)
    statement_timeout_ms: int = Field(default=30_000, ge=100)


class ObjectStorageSettings(SettingsSlice):
    bucket: str
    region: str = "us-east-1"
    endpoint_url: str | None = None  # MinIO / S3-compatible stores
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    kms_key_id: str | None = None  # enables SSE-KMS
    presign_max_seconds: int = Field(default=900, ge=1, le=3600)


class RateLimitSettings(SettingsSlice):
    redis_url: SecretStr | None = None  # absent: per-instance limiter only
    socket_timeout_seconds: float = Field(default=0.2, gt=0)


class LoggingSettings(SettingsSlice):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class JobWorkerSettings(SettingsSlice):
    # queue name -> max concurrent jobs (failure isolation per concern); at least one queue
    queues: dict[str, Annotated[int, Field(ge=1)]] = Field(min_length=1)
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    lease_seconds: int = Field(default=60, ge=5)
    heartbeat_seconds: int = Field(default=15, ge=1)
    backoff_base_seconds: float = Field(default=5.0, gt=0)
    backoff_cap_seconds: float = Field(default=900.0, gt=0)
    # ADR-011: how often a worker returns expired leases to the queue (or the dead state)
    reclaim_interval_seconds: float = Field(default=30.0, gt=0)
    # ADR-011: max jobs of ONE tenant running in one queue across all workers (None: no cap)
    max_running_per_tenant: int | None = Field(default=None, ge=1)

    @field_validator("heartbeat_seconds")
    @classmethod
    def _heartbeat_before_lease_expiry(cls, value: int, info: ValidationInfo) -> int:
        lease = info.data.get("lease_seconds")
        if lease is not None and value >= lease:
            raise ValueError("heartbeat_seconds must be shorter than lease_seconds")
        return value

    @field_validator("backoff_cap_seconds")
    @classmethod
    def _cap_not_below_base(cls, value: float, info: ValidationInfo) -> float:
        base = info.data.get("backoff_base_seconds")
        if base is not None and value < base:
            raise ValueError("backoff_cap_seconds must not be below backoff_base_seconds")
        return value
