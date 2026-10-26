"""ADR-034: kernel markers are pure metadata."""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from lda.shared.kernel.declarations import (
    Audited,
    NotAudited,
    Public,
    Requires,
    audit_of,
    audited,
    authorization_of,
    not_audited,
    public,
    requires,
)


def test_requires_records_opaque_requirements_and_returns_the_same_class() -> None:
    class Plain:
        def execute(self) -> int:
            return 1

    original = Plain
    decorated = requires("identity.disable", "doc_read")(Plain)
    assert decorated is original  # nothing wrapped
    assert authorization_of(decorated) == Requires(("identity.disable", "doc_read"))
    assert decorated().execute() == 1  # behaviour untouched


def test_public_and_audit_records() -> None:
    @public(reason="health probe")
    @not_audited(reason="high-volume noise")
    class A: ...

    @requires("a.b")
    @audited("a.b")
    class B: ...

    assert authorization_of(A) == Public("health probe")
    assert audit_of(A) == NotAudited("high-volume noise")
    assert authorization_of(B) == Requires(("a.b",))
    assert audit_of(B) == Audited("a.b")


def test_undeclared_class_has_no_declarations() -> None:
    class Bare: ...

    assert authorization_of(Bare) is None
    assert audit_of(Bare) is None


def test_declarations_are_not_inherited() -> None:
    @requires("a.b")
    @audited("a.b")
    class Parent: ...

    class Child(Parent): ...

    assert authorization_of(Child) is None
    assert audit_of(Child) is None


def test_records_are_frozen() -> None:
    record = Requires(("a.b",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.requirements = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad", ["", "Doc", "a b", "a..b", ".a", "a.", "1a", "a-b", "a" * 129, "a\n"]
)
def test_requirement_shape_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="requirement"):
        requires(bad)
    with pytest.raises(ValueError, match="action"):
        audited(bad)


def test_requirement_shape_boundary_is_accepted() -> None:
    requires("a" * 128)
    requires("a", "a.b", "a_1.b_2.c3")


def test_requires_rejects_empty_duplicate_and_non_string() -> None:
    with pytest.raises(ValueError, match="at least one"):
        requires()
    with pytest.raises(ValueError, match="duplicate"):
        requires("a.b", "a.b")
    with pytest.raises(TypeError):
        requires(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        audited(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", "a\tb", "x" * 201])
def test_reason_shape_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="reason"):
        public(reason=bad)
    with pytest.raises(ValueError, match="reason"):
        not_audited(reason=bad)


def test_reason_is_keyword_only() -> None:
    for marker in (public, not_audited):
        first = next(iter(inspect.signature(marker).parameters.values()))
        assert first.kind is inspect.Parameter.KEYWORD_ONLY


def test_markers_apply_to_classes_only() -> None:
    def func() -> None: ...

    for marker in (requires("a.b"), public(reason="r"), audited("a.b"), not_audited(reason="r")):
        with pytest.raises(TypeError, match="classes only"):
            marker(func)  # type: ignore[type-var]


def test_one_declaration_of_each_kind_per_class() -> None:
    with pytest.raises(TypeError, match="already carries"):

        @requires("a.b")
        @public(reason="r")
        class Both: ...

    with pytest.raises(TypeError, match="already carries"):

        @audited("a.b")
        @not_audited(reason="r")
        class BothAudit: ...

    with pytest.raises(TypeError, match="already carries"):

        @requires("a.b")
        @requires("c.d")
        class Twice: ...
