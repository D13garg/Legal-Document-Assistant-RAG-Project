from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class IdGenerator(Protocol):
    def new_uuid(self) -> UUID: ...
