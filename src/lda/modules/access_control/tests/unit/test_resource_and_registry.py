from __future__ import annotations

import pytest

from lda.modules.access_control.api import ActionId, ResourceKindDefinition, ResourceRef
from lda.modules.access_control.domain.errors import (
    InvalidAction,
    InvalidRegistration,
    InvalidResourceRef,
)
from lda.modules.access_control.domain.registry import ResourceRegistry
from lda.modules.access_control.testing import a_document_kind, a_resource_registry

READ = ActionId("documents.read_document")


@pytest.mark.parametrize("kind", ["", "Document", "1doc", "doc ument", "doc\n", "d" * 65, "doc.x"])
def test_invalid_kinds_are_rejected(kind: str) -> None:
    with pytest.raises(InvalidResourceRef):
        ResourceRef(kind, "abc")


@pytest.mark.parametrize(
    "resource_id", ["", " ", "-abc", "a b", "a/b", "a'b", "a\n", "x" * 129, "é"]
)
def test_invalid_resource_ids_are_rejected(resource_id: str) -> None:
    with pytest.raises(InvalidResourceRef):
        ResourceRef("document", resource_id)


@pytest.mark.parametrize(
    "resource_id", ["a", "0a", "3f2c1e9e-7d3b-4c0a-9b8a-111111111111", "a.b:c_d-e", "x" * 128]
)
def test_valid_resource_ids(resource_id: str) -> None:
    assert ResourceRef("document", resource_id).id == resource_id


@pytest.mark.parametrize(
    "action", ["", "Read", "documents.", ".read", "documents..read", "documents.read\n", "a" * 129]
)
def test_invalid_actions_are_rejected(action: str) -> None:
    with pytest.raises(InvalidAction):
        ActionId(action)


def test_non_strings_are_rejected() -> None:
    with pytest.raises(InvalidAction):
        ActionId(5)  # type: ignore[arg-type]
    with pytest.raises(InvalidResourceRef):
        ResourceRef(None, "x")  # type: ignore[arg-type]


def test_registry_allows_only_registered_pairs() -> None:
    registry = a_resource_registry()
    assert registry.allows("document", READ)
    assert not registry.allows("document", ActionId("documents.purge_document"))
    assert not registry.allows("folder", READ)


def test_registry_must_be_sealed_before_lookups() -> None:
    registry = ResourceRegistry()
    registry.register(a_document_kind())
    with pytest.raises(InvalidRegistration) as caught:
        registry.allows("document", READ)
    assert caught.value.code == "resource_registry_not_sealed"


def test_registry_rejects_duplicates_and_late_registration() -> None:
    registry = ResourceRegistry()
    registry.register(a_document_kind())
    with pytest.raises(InvalidRegistration) as dup:
        registry.register(a_document_kind())
    assert dup.value.code == "resource_kind_registered"
    registry.seal()
    with pytest.raises(InvalidRegistration) as late:
        registry.register(ResourceKindDefinition("folder", frozenset({READ})))
    assert late.value.code == "resource_registry_sealed"


def test_definitions_need_a_valid_kind_and_actions() -> None:
    with pytest.raises(InvalidRegistration):
        ResourceKindDefinition("Bad Kind", frozenset({READ}))
    with pytest.raises(InvalidRegistration):
        ResourceKindDefinition("document", frozenset())
