"""Sensitive[T]: a value that refuses to leak through repr, str, formatting or serialization."""

from __future__ import annotations

from typing import Generic, NoReturn, TypeVar

T = TypeVar("T")
_MASK = "Sensitive(***)"


class Sensitive(Generic[T]):  # noqa: UP046 - explicit Generic keeps the class usable with __slots__
    __slots__ = ("_value",)

    def __init__(self, value: T) -> None:
        object.__setattr__(self, "_value", value)

    def reveal(self) -> T:
        """The only way to read the value. Call sites are greppable and reviewable."""
        value: T = object.__getattribute__(self, "_value")
        return value

    def __repr__(self) -> str:
        return _MASK

    def __str__(self) -> str:
        return _MASK

    def __format__(self, spec: str) -> str:
        return _MASK

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("Sensitive values are immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("Sensitive values are immutable")

    def __reduce__(self) -> NoReturn:
        raise TypeError("Sensitive values cannot be pickled or serialized")

    def __copy__(self) -> Sensitive[T]:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Sensitive[T]:
        return self

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Sensitive) and self.reveal() == other.reveal()

    def __hash__(self) -> int:
        return hash(("Sensitive", self.reveal()))
