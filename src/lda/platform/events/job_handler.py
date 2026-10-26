from __future__ import annotations

from lda.platform.events.codec import EventCodec
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ValidationError
from lda.shared.ports.event_handlers import EventHandler
from lda.shared.ports.jobs import ClaimedJob, Failed, JobOutcome, Succeeded
from lda.shared.ports.unit_of_work import UnitOfWorkFactory


class EventJobHandler:
    """Runs one async event handler for one job, in its own tenant-bound transaction."""

    def __init__(self, handler: EventHandler, codec: EventCodec, uows: UnitOfWorkFactory) -> None:
        self._handler = handler
        self._codec = codec
        self._uows = uows

    def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome:
        try:
            event = self._codec.decode(job.payload)
        except ValidationError:
            return Failed(error_code="event_decode_failed")
        if event.tenant_id != job.tenant_id:
            return Failed(error_code="event_tenant_mismatch")
        with self._uows.begin(ctx) as uow:
            self._handler.handle(event, uow)
            uow.commit()
        return Succeeded()
