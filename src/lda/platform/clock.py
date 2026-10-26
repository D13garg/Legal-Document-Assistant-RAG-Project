from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class RandomIdGenerator:
    def new_uuid(self) -> UUID:
        return uuid4()
