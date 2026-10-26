from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from lda.shared.kernel.ids import TenantId

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._=-]*")  # used with fullmatch: `$` would admit "\n"
MAX_RENDERED_KEY_LENGTH = 1024  # S3 limit; longer keys can never be stored


@dataclass(frozen=True, slots=True)
class TenantScopedKey:
    """An object key that cannot exist without a tenant. `path` is relative to the tenant."""

    tenant_id: TenantId
    path: str

    def __post_init__(self) -> None:
        segments = self.path.split("/")
        if not all(_SEGMENT.fullmatch(s) for s in segments) or ".." in segments:
            raise ValueError("invalid object path")
        if len(self.render()) > MAX_RENDERED_KEY_LENGTH:
            raise ValueError("object path too long")

    def render(self) -> str:
        return f"tenant/{self.tenant_id}/{self.path}"


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: TenantScopedKey
    size: int


class ObjectStore(Protocol):
    def put(
        self, key: TenantScopedKey, body: bytes, *, content_type: str, overwrite: bool = False
    ) -> StoredObject:
        """Originals are immutable: overwrite=False raises ConflictError if the key exists."""
        ...

    def get(self, key: TenantScopedKey) -> bytes:
        """Raises NotFoundError when absent."""
        ...

    def exists(self, key: TenantScopedKey) -> bool: ...

    def delete(self, key: TenantScopedKey) -> None:
        """Idempotent: deleting an absent key succeeds."""
        ...

    def presign_get(self, key: TenantScopedKey, *, expires_seconds: int) -> str: ...
