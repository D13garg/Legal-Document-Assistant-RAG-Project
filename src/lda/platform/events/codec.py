from __future__ import annotations

import dataclasses
import typing
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from types import UnionType
from uuid import UUID

from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import EntityId
from lda.shared.ports.jobs import JsonValue

_PRIMITIVES = (str, int, float, bool)


def _matches_primitive(tp: object, raw: JsonValue) -> bool:
    if tp is bool:
        return isinstance(raw, bool)
    if tp is int:
        return isinstance(raw, int) and not isinstance(raw, bool)
    if tp is float:
        return isinstance(raw, int | float) and not isinstance(raw, bool)
    return isinstance(raw, str)


class EventCodec:
    """JSON round-trip for registered events. Supports a deliberately small set of field types
    (primitives, UUID, aware datetime, Enum, EntityId subclasses, and `X | None`) so that events
    stay flat, identifier-only facts."""

    def __init__(self) -> None:
        self._types: dict[str, type[DomainEvent]] = {}

    def register(self, event_type: type[DomainEvent]) -> None:
        name = event_type.type_name
        if not name:
            raise ValueError(f"{event_type.__name__} must define a non-empty type_name")
        if name in self._types and self._types[name] is not event_type:
            raise ValueError(f"duplicate event type_name {name}")
        hints = typing.get_type_hints(event_type)
        for f in dataclasses.fields(event_type):
            self._check_supported(hints[f.name], event_type)
        self._types[name] = event_type

    def encode(self, event: DomainEvent) -> dict[str, JsonValue]:
        if self._types.get(event.type_name) is not type(event):
            raise ValidationError("event_not_registered", detail=type(event).__name__)
        data = {
            f.name: self._encode_value(getattr(event, f.name)) for f in dataclasses.fields(event)
        }
        return {"type": event.type_name, "data": data}

    def decode(self, payload: Mapping[str, JsonValue]) -> DomainEvent:
        type_name = payload.get("type")
        data = payload.get("data")
        event_type = self._types.get(type_name) if isinstance(type_name, str) else None
        if event_type is None or not isinstance(data, dict):
            raise ValidationError("event_decode_failed")
        hints = typing.get_type_hints(event_type)
        try:
            kwargs: dict[str, typing.Any] = {
                f.name: self._decode_value(hints[f.name], data[f.name])
                for f in dataclasses.fields(event_type)
            }
            return event_type(**kwargs)
        except (KeyError, ValueError, TypeError):
            raise ValidationError("event_decode_failed") from None

    def _check_supported(self, tp: object, owner: type) -> None:
        if tp is type(None) or tp in _PRIMITIVES or tp is UUID or tp is datetime:
            return
        if isinstance(tp, type) and issubclass(tp, Enum | EntityId):
            return
        origin = typing.get_origin(tp)
        if origin in (typing.Union, UnionType):
            args = typing.get_args(tp)
            if sum(a is not type(None) for a in args) != 1:
                raise ValueError(f"{owner.__name__}: only `X | None` unions are supported")
            for arg in args:
                self._check_supported(arg, owner)
            return
        raise ValueError(f"{owner.__name__}: unsupported event field type {tp!r}")

    def _encode_value(self, value: object) -> JsonValue:
        if value is None or isinstance(value, _PRIMITIVES):
            return value
        if isinstance(value, EntityId):
            return str(value.value)
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return str(value.value)
        raise ValueError("unsupported value")

    def _decode_value(self, tp: object, raw: JsonValue) -> object:
        if raw is None:
            return None
        origin = typing.get_origin(tp)
        if origin in (typing.Union, UnionType):
            args = [a for a in typing.get_args(tp) if a is not type(None)]
            return self._decode_value(args[0], raw)
        if tp is UUID:
            return UUID(str(raw))
        if tp is datetime:
            return datetime.fromisoformat(str(raw))
        if isinstance(tp, type) and issubclass(tp, EntityId):
            return tp(UUID(str(raw)))
        if isinstance(tp, type) and issubclass(tp, Enum):
            return tp(raw)
        if not _matches_primitive(tp, raw):
            raise ValueError("payload value does not match the declared field type")
        return raw
