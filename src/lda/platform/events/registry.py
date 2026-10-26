from __future__ import annotations

from dataclasses import dataclass

from lda.shared.kernel.errors import ConflictError
from lda.shared.kernel.events import DomainEvent
from lda.shared.ports.event_handlers import EventHandler
from lda.shared.ports.events import Delivery

EVENT_QUEUE = "events"


@dataclass(frozen=True, slots=True)
class Registration:
    event_type: type[DomainEvent]
    handler: EventHandler
    delivery: Delivery


class HandlerRegistry:
    """Explicit event -> handler wiring, populated by bootstrap. Exact type match only."""

    def __init__(self) -> None:
        self._registrations: list[Registration] = []

    def register(
        self, event_type: type[DomainEvent], handler: EventHandler, delivery: Delivery
    ) -> None:
        if any(
            r.handler.name == handler.name and r.delivery == delivery for r in self._registrations
        ):
            raise ConflictError("handler_already_registered", detail=handler.name)
        self._registrations.append(Registration(event_type, handler, delivery))

    def handlers_for(
        self, event_type: type[DomainEvent], delivery: Delivery
    ) -> tuple[EventHandler, ...]:
        return tuple(
            r.handler
            for r in self._registrations
            if r.event_type is event_type and r.delivery == delivery
        )

    def registrations(self, delivery: Delivery) -> tuple[Registration, ...]:
        return tuple(r for r in self._registrations if r.delivery == delivery)
