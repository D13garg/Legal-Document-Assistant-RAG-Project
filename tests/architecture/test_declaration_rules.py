"""AG053 (ADR-034): markers are class-level, kernel-imported, literal and shape-checked."""

from __future__ import annotations

import pytest

from archguard import policy
from lda.shared.kernel import declarations
from repo_builder import MakeRepo

UC = "application/use_cases/x.py"
IMPORTS = "from lda.shared.kernel.declarations import audited, not_audited, public, requires\n"
BODY = "class Do:\n    def execute(self): ...\n"


def src(authz: str, audit: str = "@audited('doc.read')\n", imports: str = IMPORTS) -> str:
    return imports + authz + audit + BODY


def ag053(repo_files: dict[str, str], make_repo: MakeRepo) -> list[str]:
    repo = make_repo()
    for rel, content in repo_files.items():
        repo.module_file("a", rel, content)
    return [v for v in repo.violations() if v == "AG053"]


VALID = [
    src("@requires('doc.read')\n"),
    src("@requires('doc.read', 'doc.list')\n"),
    src("@public(reason='health probe')\n", "@not_audited(reason='noise')\n"),
    src("@requires('doc.read')\n", "@audited(action='doc.read')\n"),
    src(
        "@d.requires('doc.read')\n",
        "@d.audited('doc.read')\n",
        "from lda.shared.kernel import declarations as d\n",
    ),
    src(
        "@req('doc.read')\n",
        "@audited('doc.read')\n",
        "from lda.shared.kernel.declarations import audited, requires as req\n",
    ),
]


@pytest.mark.parametrize("content", VALID)
def test_valid_declarations_pass(make_repo: MakeRepo, content: str) -> None:
    assert ag053({UC: content}, make_repo) == []


INVALID = {
    "missing authorization": IMPORTS + "@audited('a.b')\n" + BODY,
    "missing audit": IMPORTS + "@requires('a.b')\n" + BODY,
    "both authorization kinds": src("@requires('a.b')\n@public(reason='r')\n"),
    "both audit kinds": src("@requires('a.b')\n", "@audited('a.b')\n@not_audited(reason='r')\n"),
    "requires twice": src("@requires('a.b')\n@requires('c.d')\n"),
    "bare requires": src("@requires\n"),
    "requires without arguments": src("@requires()\n"),
    "requires non-literal": src("@requires(NAME)\n"),
    "requires bad shape": src("@requires('Doc Read')\n"),
    "requires too long": src(f"@requires('{'a' * 129}')\n"),
    "requires duplicate": src("@requires('a.b', 'a.b')\n"),
    "requires keyword": src("@requires(name='a.b')\n"),
    "requires star args": src("@requires(*NAMES)\n"),
    "public without reason": src("@public()\n"),
    "public blank reason": src("@public(reason='  ')\n"),
    "public positional reason": src("@public('because')\n"),
    "public non-literal reason": src("@public(reason=WHY)\n"),
    "public multiline reason": src("@public(reason='a\\nb')\n"),
    "public reason too long": src(f"@public(reason='{'x' * 201}')\n"),
    "not_audited without reason": src("@requires('a.b')\n", "@not_audited()\n"),
    "audited bad shape": src("@requires('a.b')\n", "@audited('Do It')\n"),
    "audited two arguments": src("@requires('a.b')\n", "@audited('a.b', 'c.d')\n"),
    "audited wrong keyword": src("@requires('a.b')\n", "@audited(label='a.b')\n"),
    "locally defined marker": (
        "def requires(*a): ...\ndef audited(a): ...\n@requires('a.b')\n@audited('a.b')\n" + BODY
    ),
    "marker from another module": src(
        "@requires('a.b')\n", imports="from other import audited, requires\n"
    ),
    "method level requires": IMPORTS
    + "@audited('a.b')\nclass Do:\n    @requires('a.b')\n    def execute(self): ...\n",
    "method level audited": IMPORTS
    + "@requires('a.b')\nclass Do:\n    @audited('a.b')\n    def execute(self): ...\n",
    "marker on private class": src("@requires('a.b')\n")
    + "@public(reason='r')\nclass _Helper: ...\n",
    "marker on module function": src("@requires('a.b')\n") + "@public(reason='r')\ndef f(): ...\n",
}


@pytest.mark.parametrize("name", list(INVALID))
def test_invalid_declarations_are_rejected(make_repo: MakeRepo, name: str) -> None:
    assert ag053({UC: INVALID[name]}, make_repo), name


def test_queries_directory_without_declarations_is_rejected(make_repo: MakeRepo) -> None:
    assert ag053({"application/queries/x.py": IMPORTS + BODY}, make_repo)


def test_queries_directory_with_declarations_passes(make_repo: MakeRepo) -> None:
    assert ag053({"application/queries/x.py": src("@requires('a.b')\n")}, make_repo) == []


@pytest.mark.parametrize(
    "rel",
    ["domain/x.py", "application/x.py", "infrastructure/x.py", "api/x.py", "presentation/x.py"],
)
def test_markers_outside_use_cases_are_rejected(make_repo: MakeRepo, rel: str) -> None:
    body = IMPORTS + "@requires('a.b')\nclass Something: ...\n"
    assert ag053({rel: body}, make_repo)


def test_marker_on_a_function_outside_use_cases_is_rejected(make_repo: MakeRepo) -> None:
    assert ag053({"application/x.py": IMPORTS + "@audited('a.b')\ndef f(): ...\n"}, make_repo)


def test_unrelated_decorators_are_ignored(make_repo: MakeRepo) -> None:
    content = "import functools\n@functools.cache\ndef f(): ...\n@staticmethod\ndef g(): ...\n"
    assert ag053({"domain/x.py": content}, make_repo) == []


def test_policy_shape_limits_mirror_the_kernel() -> None:
    assert policy.DECLARATION_IDENTIFIER_PATTERN == declarations.IDENTIFIER_PATTERN
    assert policy.DECLARATION_MAX_IDENTIFIER_LENGTH == declarations.MAX_IDENTIFIER_LENGTH
    assert policy.DECLARATION_MAX_REASON_LENGTH == declarations.MAX_REASON_LENGTH
    assert declarations.__name__ == policy.DECLARATIONS_MODULE
    assert {"requires", "public"} == policy.AUTHZ_DECORATORS
    assert {"audited", "not_audited"} == policy.AUDIT_DECORATORS
    for name in policy.AUTHZ_DECORATORS | policy.AUDIT_DECORATORS:
        assert callable(getattr(declarations, name))


def test_kernel_is_adr_governed() -> None:
    from archguard import adr

    path = "src/lda/shared/kernel/declarations.py"
    assert [v.rule for v in adr.adr_gate([path])] == ["AG072"]
    assert (
        adr.adr_gate(
            [path, "docs/architecture/ADR/ADR-034-x.md", "docs/architecture/decisions-log.md"]
        )
        == []
    )
