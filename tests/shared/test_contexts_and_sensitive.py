"""System work never masquerades as user work; sensitive values do not leak."""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import pickle
from uuid import UUID

import pytest

from lda.shared.kernel.context import ExecutionContext, SystemContext, TenantContext
from lda.shared.kernel.ids import JobId, PrincipalId, RequestId, TenantId
from lda.shared.kernel.sensitive import Sensitive

T1 = TenantId(UUID(int=1))
SECRET = "hunter2-PRIVILEGED"  # noqa: S105 - fixture value


def tenant_ctx() -> TenantContext:
    return TenantContext(T1, PrincipalId(UUID(int=7)), RequestId(UUID(int=8)))


def test_system_and_tenant_contexts_are_distinct_types() -> None:
    user = tenant_ctx()
    system = SystemContext(tenant_id=T1, purpose="job:x", job_id=JobId(UUID(int=9)))
    assert not isinstance(system, TenantContext) and not isinstance(user, SystemContext)
    assert user != system  # type: ignore[comparison-overlap]  # mypy also rejects it
    assert not hasattr(system, "principal_id") and not hasattr(system, "request_id")
    both: tuple[ExecutionContext, ...] = (user, system)
    assert [type(c).__name__ for c in both] == ["TenantContext", "SystemContext"]


def test_contexts_are_immutable() -> None:
    system = SystemContext(tenant_id=T1, purpose="job:x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        system.tenant_id = TenantId(UUID(int=2))  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        tenant_ctx().tenant_id = TenantId(UUID(int=2))  # type: ignore[misc]


@pytest.mark.parametrize("purpose", ["", " ", "\t\n"])
def test_system_context_needs_a_real_purpose(purpose: str) -> None:
    with pytest.raises(ValueError, match="purpose"):
        SystemContext(tenant_id=T1, purpose=purpose)


def test_sensitive_is_masked_in_every_text_rendering() -> None:
    s = Sensitive(SECRET)
    renderings = [
        repr(s),
        str(s),
        f"{s}",
        f"{s!r}",
        f"{s:>40}",
        "%s" % s,  # noqa: UP031 - the %-format path is exactly what is being tested
        "%r" % s,  # noqa: UP031
        "{}".format(s),  # noqa: UP032
        "{:^30}".format(s),  # noqa: UP032
        repr([s]),
        repr({"k": s}),
        repr((s,)),
        str(ValueError(s)),
        repr(ValueError(s)),
    ]
    assert all(SECRET not in text for text in renderings)


def test_sensitive_inside_a_dataclass_is_masked() -> None:
    @dataclasses.dataclass
    class Holder:
        token: Sensitive[str]

    assert SECRET not in repr(Holder(Sensitive(SECRET)))
    assert SECRET not in str(dataclasses.astuple(Holder(Sensitive(SECRET))))


def test_sensitive_cannot_be_serialised_copied_into_state_or_introspected() -> None:
    s = Sensitive(SECRET)
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError):
            pickle.dumps(s, protocol=protocol)
    with pytest.raises(TypeError):
        json.dumps({"x": s})
    assert SECRET not in json.dumps({"x": s}, default=str)  # even a lenient encoder masks it
    assert not hasattr(s, "__dict__")  # __slots__: no attribute dict to dump
    assert copy.copy(s) is s and copy.deepcopy(s) is s
    with pytest.raises(AttributeError):
        s.reveal = lambda: "x"  # type: ignore[method-assign]
    with pytest.raises(AttributeError):
        delattr(s, "_value")
    assert s.reveal() == SECRET  # the single sanctioned accessor


def test_sensitive_does_not_leak_through_the_logging_machinery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        logging.getLogger("test.sensitive").info(
            "value=%s repr=%r", Sensitive(SECRET), Sensitive(SECRET)
        )
    assert SECRET not in caplog.text


def test_sensitive_equality_requires_both_sides_to_be_sensitive() -> None:
    assert Sensitive("a") == Sensitive("a") and Sensitive("a") != Sensitive("b")
    assert Sensitive("a") != "a"  # comparing with the raw value never succeeds
