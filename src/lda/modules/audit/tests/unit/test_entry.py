"""The domain value that decides whether a fact is an acceptable audit record (ADR-019 D6)."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from lda.modules.audit.application.entry_from_fact import audit_entry_of
from lda.modules.audit.domain.entry import (
    FORBIDDEN_KEY_WORDS,
    OUTCOMES,
    RESERVED_PURPOSE_KEY,
)
from lda.modules.audit.domain.errors import InvalidAuditEntry
from lda.modules.audit.tests.conftest import ACTOR, REQUEST, TENANT_A, TENANT_B, a_fact
from lda.shared.kernel.declarations import IDENTIFIER_PATTERN, MAX_IDENTIFIER_LENGTH
from lda.shared.ports.audit import AuditOutcome


def code_of(**over: object) -> str:
    with pytest.raises(InvalidAuditEntry) as caught:
        audit_entry_of(a_fact(**over))
    assert caught.value.detail == ""  # a rejected fact is never echoed
    return caught.value.code


def test_a_valid_fact_maps_field_for_field() -> None:
    entry = audit_entry_of(a_fact())
    assert (entry.tenant_id, entry.actor, entry.request_id) == (TENANT_A, ACTOR, REQUEST)
    assert entry.action == "identity.principal_disabled"
    assert (entry.resource_type, entry.outcome) == ("principal", "success")
    assert dict(entry.attributes) == {"principal_kind": "user"}


def test_system_work_has_no_actor_and_no_request_id() -> None:
    entry = audit_entry_of(a_fact(actor=None, request_id=None, resource_id=None))
    assert (entry.actor, entry.request_id, entry.resource_id) == (None, None, None)


def test_outcomes_equal_the_ports_outcomes() -> None:
    assert {o.value for o in AuditOutcome} == OUTCOMES


def test_the_domain_does_not_import_ports_or_platform() -> None:
    import ast
    import pathlib

    import lda.modules.audit.domain as domain

    for path in pathlib.Path(domain.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("lda.shared.ports", "lda.platform")), path


@pytest.mark.parametrize("outcome", list(AuditOutcome))
def test_every_port_outcome_is_accepted(outcome: AuditOutcome) -> None:
    assert audit_entry_of(a_fact(outcome=outcome)).outcome == outcome.value


@pytest.mark.parametrize(
    "action",
    ["", "Do.It", "a b", "1a", "a..b", "a." + "b" * MAX_IDENTIFIER_LENGTH, "a\n", "a-b"],
)
def test_actions_must_be_declaration_identifiers(action: str) -> None:
    assert code_of(action=action) == "audit_action_invalid"


def test_action_length_boundary() -> None:
    audit_entry_of(a_fact(action="a" * MAX_IDENTIFIER_LENGTH))
    assert code_of(action="a" * (MAX_IDENTIFIER_LENGTH + 1)) == "audit_action_invalid"


@pytest.mark.parametrize(
    "action", ["a.b", "a_b.c1", "A.b", "a..b", "a.", ".a", "a-b", "a b", "9a", "a.b.c.d", "é.x"]
)
def test_action_shape_is_the_kernels_single_source(action: str) -> None:
    kernel_accepts = re.fullmatch(IDENTIFIER_PATTERN, action) is not None
    try:
        audit_entry_of(a_fact(action=action))
        domain_accepts = True
    except InvalidAuditEntry:
        domain_accepts = False
    assert domain_accepts == kernel_accepts


@pytest.mark.parametrize("value", ["", "Principal", "a.b", "a" * 65, "1a", "a b", "a\n"])
def test_resource_types_are_resource_kinds(value: str) -> None:
    assert code_of(resource_type=value) == "audit_resource_type_invalid"


@pytest.mark.parametrize("value", ["", ".x", "a b", "a/b", "a" * 129, "a\n", "é"])
def test_resource_ids_are_opaque_identifiers_not_text(value: str) -> None:
    assert code_of(resource_id=value) == "audit_resource_id_invalid"


def test_resource_id_boundary() -> None:
    audit_entry_of(a_fact(resource_id="a" * 128))
    assert code_of(resource_id="a" * 129) == "audit_resource_id_invalid"


@pytest.mark.parametrize("value", ["", "ok", "SUCCESS", "unknown"])
def test_only_known_outcomes_are_accepted(value: str) -> None:
    with pytest.raises(InvalidAuditEntry) as caught:
        replace(audit_entry_of(a_fact()), outcome=value)
    assert caught.value.code == "audit_outcome_invalid"


def test_naive_time_is_rejected_and_aware_time_in_any_zone_is_accepted() -> None:
    assert code_of(occurred_at=datetime(2026, 1, 1)) == "audit_time_naive"
    zone = timezone(timedelta(hours=5, minutes=30))
    audit_entry_of(a_fact(occurred_at=datetime(2026, 1, 1, tzinfo=zone)))
    audit_entry_of(a_fact(occurred_at=datetime(2026, 1, 1, tzinfo=UTC)))


def test_a_fact_for_another_tenant_is_rejected() -> None:
    entry = audit_entry_of(a_fact())
    entry.require_tenant(TENANT_A)
    with pytest.raises(InvalidAuditEntry) as caught:
        entry.require_tenant(TENANT_B)
    assert caught.value.code == "audit_tenant_mismatch"


def test_sixteen_attributes_are_allowed_and_seventeen_are_not() -> None:
    audit_entry_of(a_fact(attributes={f"k{i}": "v" for i in range(16)}))
    assert code_of(attributes={f"k{i}": "v" for i in range(17)}) == "audit_attributes_too_many"


@pytest.mark.parametrize("key", ["", "K", "a b", "1a", "a-b", "a\n", "é", "a" * 65, ".a"])
def test_attribute_keys_must_be_identifiers_of_at_most_64_characters(key: str) -> None:
    assert code_of(attributes={key: "v"}) == "audit_attribute_key_invalid"


def test_attribute_key_length_boundary_and_dotted_keys() -> None:
    audit_entry_of(a_fact(attributes={"a" * 64: "v"}))
    audit_entry_of(a_fact(attributes={"principal.kind": "v"}))


@pytest.mark.parametrize("value", ["a\nb", "a\tb", "a\x00b", "a\rb", "a\x1bb", "a\u2028b"])
def test_attribute_values_must_be_printable_and_single_line(value: str) -> None:
    assert code_of(attributes={"k": value}) == "audit_attribute_value_invalid"


def test_attribute_value_length_boundary() -> None:
    audit_entry_of(a_fact(attributes={"k": "x" * 256}))
    assert code_of(attributes={"k": "x" * 257}) == "audit_attribute_value_invalid"


def test_non_string_attribute_values_are_rejected() -> None:
    assert code_of(attributes={"k": 1}) == "audit_attribute_value_invalid"
    assert code_of(attributes={"k": None}) == "audit_attribute_value_invalid"


@pytest.mark.parametrize("word", sorted(FORBIDDEN_KEY_WORDS))
@pytest.mark.parametrize("shape", ["{w}", "x_{w}", "{w}_x", "x.{w}.y", "x_{w}_y"])
def test_content_bearing_attribute_names_are_refused(word: str, shape: str) -> None:
    assert code_of(attributes={shape.format(w=word): "v"}) == "audit_attribute_key_forbidden"


@pytest.mark.parametrize(
    "key",
    [
        "document_text",
        "chunk_text",
        "prompt",
        "model_output",
        "answer",
        "raw_token",
        "proof_token",
        "signing_key",
        "display_name",
        "password",
        "email",
    ],
)
def test_the_named_dangers_are_refused(key: str) -> None:
    assert code_of(attributes={key: "v"}) == "audit_attribute_key_forbidden"


@pytest.mark.parametrize(
    "key",
    [RESERVED_PURPOSE_KEY, "principal_id", "granted_action", "policy_epoch", "principal_kind"],
)
def test_the_keys_the_repository_emits_are_accepted(key: str) -> None:
    audit_entry_of(a_fact(attributes={key: "v"}))


def test_accepted_attributes_are_a_read_only_copy() -> None:
    source = {"k": "v"}
    entry = audit_entry_of(a_fact(attributes=source))
    source["k"] = "changed"
    source["other"] = "x"
    assert dict(entry.attributes) == {"k": "v"}
    with pytest.raises(TypeError):
        entry.attributes["k"] = "again"  # type: ignore[index]


def test_an_entry_is_immutable() -> None:
    entry = audit_entry_of(a_fact())
    with pytest.raises(AttributeError):
        entry.action = "x.y"  # type: ignore[misc]
