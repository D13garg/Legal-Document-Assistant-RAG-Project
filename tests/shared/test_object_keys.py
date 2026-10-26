"""Object-storage keys are tenant-scoped, and path traversal / malformed paths are rejected."""

from __future__ import annotations

import dataclasses
import posixpath
import random
from uuid import UUID

import pytest

from lda.shared.kernel.ids import TenantId
from lda.shared.ports.object_store import MAX_RENDERED_KEY_LENGTH, TenantScopedKey

T1, T2 = TenantId(UUID(int=1)), TenantId(UUID(int=2))

TRAVERSAL = [
    "..",
    "../x",
    "a/..",
    "a/../b",
    "a/../../b",
    "./a",
    "a/./b",
    ".",
    "a/.",
    "....//x",
    ".hidden",
    "a/.hidden",
]
MALFORMED = [
    "",
    "/",
    "/abs",
    "a/",
    "a//b",
    "a\\b",
    "..\\x",
    "a b",
    " a",
    "a ",
    "a\tb",
    "a\nb",
    "a\n",  # `$` in a regex would accept a trailing newline
    "a/b\n",
    "a\r",
    "a\x00b",
    "a%2e%2e/b",
    "%2e%2e/b",
    "a?x=1",
    "a#frag",
    "a:b",
    "a*",
    "~root",
    "é",
    "a/é",
    "a\u202eb",  # right-to-left override
    "\uff0e\uff0e/x",  # fullwidth full stops
]


@pytest.mark.parametrize("path", TRAVERSAL)
def test_traversal_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError, match="invalid object path"):
        TenantScopedKey(T1, path)


@pytest.mark.parametrize("path", MALFORMED)
def test_malformed_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError, match="invalid object path"):
        TenantScopedKey(T1, path)


def test_overlong_paths_are_rejected_at_the_s3_key_limit() -> None:
    prefix_length = len(TenantScopedKey(T1, "x").render()) - 1
    longest = "a" * (MAX_RENDERED_KEY_LENGTH - prefix_length)
    assert len(TenantScopedKey(T1, longest).render()) == MAX_RENDERED_KEY_LENGTH
    with pytest.raises(ValueError, match="too long"):
        TenantScopedKey(T1, longest + "a")


@pytest.mark.parametrize(
    "path", ["a", "documents/d1/original", "a.b-c_d=e", "0/1/2", "a..b", "v1.2.3/file.pdf"]
)
def test_valid_paths_are_accepted(path: str) -> None:
    assert TenantScopedKey(T1, path).render() == f"tenant/{T1}/{path}"


def test_replace_revalidates_so_a_valid_key_cannot_be_mutated_into_traversal() -> None:
    key = TenantScopedKey(T1, "a/b")
    with pytest.raises(ValueError):
        dataclasses.replace(key, path="../b")
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.path = "../b"  # type: ignore[misc]


def test_same_path_in_two_tenants_never_collides() -> None:
    assert TenantScopedKey(T1, "d/original").render() != TenantScopedKey(T2, "d/original").render()


def test_a_path_cannot_impersonate_another_tenant_prefix() -> None:
    key = TenantScopedKey(T1, f"tenant/{T2}/d/original")
    assert key.render().startswith(f"tenant/{T1}/")  # still inside tenant 1's namespace


def test_randomised_paths_never_escape_the_tenant_prefix() -> None:
    rng = random.Random(1234)  # noqa: S311 - deterministic test data, not security
    alphabet = ["a", "b", "0", ".", "..", "/", "\n", " ", "\\", "%", "-", "_", "="]
    accepted = 0
    for _ in range(5000):
        path = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8)))
        try:
            rendered = TenantScopedKey(T1, path).render()
        except ValueError:
            continue
        accepted += 1
        prefix = f"tenant/{T1}/"
        assert rendered.startswith(prefix)
        assert posixpath.normpath(rendered) == rendered  # nothing for a filesystem to collapse
        assert not any(c in rendered for c in "\n\r\\\x00") and ".." not in rendered.split("/")
    assert accepted > 0  # the generator really produced valid paths too
