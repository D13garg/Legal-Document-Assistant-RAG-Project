from __future__ import annotations

from typing import Protocol

from lda.shared.kernel.events import DomainEvent
from lda.shared.ports.unit_of_work import UnitOfWork


class EventHandler(Protocol):
    """Reacts to one event type. `name` is stable: it is part of the async job identity."""

    @property
    def name(self) -> str: ...

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None: ...
