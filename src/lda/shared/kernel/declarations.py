"""Use-case declaration markers (ADR-034): pure metadata, no behaviour.

Each decorator validates the *shape* of its arguments, attaches one frozen record to the class and
returns the same class unchanged: nothing is wrapped, nothing is enforced at call time. What a
requirement string *means*, and whether a caller satisfies it, is decided elsewhere (access_control,
ADR-014); how an audit label is recorded is decided by the audit module (ADR-019).

The shape limits below are mirrored in ``tools/archguard/policy.py`` (a test keeps them equal).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

IDENTIFIER_PATTERN = r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*"
MAX_IDENTIFIER_LENGTH = 128
MAX_REASON_LENGTH = 200

_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)
_AUTHORIZATION_ATTR = "__lda_authorization__"
_AUDIT_ATTR = "__lda_audit__"


def _identifier(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string")
    if len(value) > MAX_IDENTIFIER_LENGTH or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(
            f"{what} must be a lowercase dotted identifier "
            f"of at most {MAX_IDENTIFIER_LENGTH} characters"
        )
    return value


def _reason(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string")
    if not value.strip() or len(value) > MAX_REASON_LENGTH or not value.isprintable():
        raise ValueError(
            f"{what} must be non-empty, printable, single-line text of at most "
            f"{MAX_REASON_LENGTH} characters"
        )
    return value


@dataclass(frozen=True, slots=True)
class Requires:
    """The use case declares opaque requirements. The kernel never interprets them."""

    requirements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Public:
    """The use case deliberately declares no authorization requirement."""

    reason: str


@dataclass(frozen=True, slots=True)
class Audited:
    """The use case is auditable under this action label."""

    action: str


@dataclass(frozen=True, slots=True)
class NotAudited:
    """The use case is deliberately not audited."""

    reason: str


class ClassMarker(Protocol):
    """What every marker decorator returns: a class decorator that hands the class back."""

    def __call__[C: type](self, cls: C, /) -> C: ...


AuthorizationDeclaration = Requires | Public
AuditDeclaration = Audited | NotAudited


def _attach[C: type](cls: C, attr: str, record: object) -> C:
    if not isinstance(cls, type):
        raise TypeError("declaration markers apply to use-case classes only")
    if attr in vars(cls):
        raise TypeError(f"{cls.__name__} already carries this kind of declaration")
    setattr(cls, attr, record)
    return cls


def requires(*requirements: str) -> ClassMarker:
    """Declare one or more opaque requirements, e.g. ``@requires("identity.disable")``."""
    if not requirements:
        raise ValueError("requires() needs at least one requirement")
    checked = tuple(_identifier(r, "requirement") for r in requirements)
    if len(set(checked)) != len(checked):
        raise ValueError("requires() received a duplicate requirement")
    record = Requires(checked)

    def apply[C: type](cls: C) -> C:
        return _attach(cls, _AUTHORIZATION_ATTR, record)

    return apply


def public(*, reason: str) -> ClassMarker:
    """Declare that no authorization requirement applies, with a mandatory reason."""
    record = Public(_reason(reason, "reason"))

    def apply[C: type](cls: C) -> C:
        return _attach(cls, _AUTHORIZATION_ATTR, record)

    return apply


def audited(action: str) -> ClassMarker:
    """Declare the audit action label, e.g. ``@audited("identity.disabled")``."""
    record = Audited(_identifier(action, "action"))

    def apply[C: type](cls: C) -> C:
        return _attach(cls, _AUDIT_ATTR, record)

    return apply


def not_audited(*, reason: str) -> ClassMarker:
    """Declare that the use case is deliberately not audited, with a mandatory reason."""
    record = NotAudited(_reason(reason, "reason"))

    def apply[C: type](cls: C) -> C:
        return _attach(cls, _AUDIT_ATTR, record)

    return apply


def authorization_of(cls: type) -> AuthorizationDeclaration | None:
    """The class's own authorization declaration (not inherited), or None if undeclared."""
    found = vars(cls).get(_AUTHORIZATION_ATTR)
    return found if isinstance(found, Requires | Public) else None


def audit_of(cls: type) -> AuditDeclaration | None:
    """The class's own audit declaration (not inherited), or None if undeclared."""
    found = vars(cls).get(_AUDIT_ATTR)
    return found if isinstance(found, Audited | NotAudited) else None
