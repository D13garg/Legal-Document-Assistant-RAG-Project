"""Step 3 checkpoint: Access Control's place in the DAG, who may mint, and what stays out of it.

These tests inspect the real repository (not fixtures). They protect decisions D1-D8
(ADR-014, ADR-026): no roles, no cross-module persistence, no ai_gateway -> access_control edge,
proofs minted only by Access Control, authorization semantics kept out of the kernel.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from archguard import policy
from archguard.manifest import load_dag, load_manifests
from archguard.runner import run_all

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
AC = SRC / "modules/access_control"
LAYERS = ("domain", "application", "api", "infrastructure", "testing")


def py_files(base: Path) -> list[Path]:
    return [p for p in base.rglob("*.py") if "__pycache__" not in p.parts]


def imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
            out += [f"{node.module}.{a.name}" for a in node.names]
    return out


def calls(path: Path, names: set[str]) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in names:
                found.add(name)
    return found


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_the_repository_passes_every_archguard_rule_with_access_control_present() -> None:
    assert run_all(ROOT) == []


# --- DAG --------------------------------------------------------------------------------------
def test_access_control_depends_on_identity_and_nothing_else() -> None:
    manifests, _ = load_manifests(ROOT)
    dag, _ = load_dag(ROOT)
    assert manifests["access_control"].depends_on == ("identity",)
    assert dag["access_control"] == ("identity",)
    for layer in (*LAYERS, "tests"):
        for path in py_files(AC / layer):
            for name in imports(path):
                other = re.match(r"lda\.modules\.(\w+)", name)
                if other and other.group(1) not in {"access_control", "identity"}:
                    raise AssertionError((rel(path), name))
                if other and other.group(1) == "identity":
                    assert re.match(r"lda\.modules\.identity\.(api|testing)(\.|$)", name), (
                        rel(path),
                        name,
                    )


def test_ai_gateway_has_no_dependency_on_access_control() -> None:
    """D6: the egress boundary verifies proofs through the shared port, never the module."""
    dag, _ = load_dag(ROOT)
    assert dag["ai_gateway"] == ()
    assert "access_control" not in dag["ai_gateway"]
    gateway = SRC / "modules/ai_gateway"
    if gateway.exists():  # step 9 creates it; from then on this scans real code
        manifests, _ = load_manifests(ROOT)
        assert "access_control" not in manifests["ai_gateway"].depends_on
        for path in py_files(gateway):
            assert not [n for n in imports(path) if n.startswith("lda.modules.access_control")], (
                rel(path)
            )
            assert not [n for n in imports(path) if n.startswith("lda.bootstrap")], rel(path)


def test_access_control_does_not_import_ai_gateway_or_any_downstream_module() -> None:
    for path in py_files(AC):
        assert not [
            n for n in imports(path) if re.match(r"lda\.modules\.(?!access_control|identity)", n)
        ], rel(path)


def test_the_verification_contract_lives_in_shared_ports_and_imports_only_the_kernel() -> None:
    port = SRC / "shared/ports/authorization.py"
    assert port.exists()
    lda_imports = [n for n in imports(port) if n.startswith("lda.")]
    assert lda_imports and all(n.startswith("lda.shared.kernel") for n in lda_imports)
    source = port.read_text(encoding="utf-8")
    assert "class ProofVerifier(Protocol)" in source
    assert not [n for n in imports(port) if "access_control" in n or "lda.modules" in n]


def test_the_concrete_verifier_is_handed_out_as_the_shared_port() -> None:
    source = (SRC / "bootstrap/access_control.py").read_text(encoding="utf-8")
    assert "proof_verifier: ProofVerifier" in source
    assert "from lda.shared.ports.authorization import ProofVerifier" in source


# --- only Access Control can mint -----------------------------------------------------------------
def test_proof_and_scope_constructors_are_restricted_to_access_control_in_policy() -> None:
    for name in ("AuthorizationProof", "AccessScope"):
        assert policy.RESTRICTED_CONSTRUCTORS[name] == ("src/lda/modules/access_control/",)
    assert "AG090" in policy.NON_WAIVABLE


def test_nothing_outside_access_control_constructs_a_proof_or_scope() -> None:
    owners = {"AuthorizationProof", "AccessScope", "ProofClaims"}
    for base in (SRC, ROOT / "tools", ROOT / "tests"):
        for path in py_files(base):
            if AC in path.parents or path.name in {
                "test_access_control_boundaries.py",
                "test_import_rules.py",
            }:
                continue
            assert not calls(path, owners), rel(path)


def test_signing_and_opening_functions_are_used_only_inside_access_control() -> None:
    """`issue_proof` mints; `open_token` reads claims. No other module, bootstrap included."""
    for path in py_files(SRC):
        if AC in path.parents:
            continue
        used = [n for n in imports(path) if n.endswith((".issue_proof", ".open_token"))]
        assert not used, rel(path)
    minting = [p for p in py_files(AC / "application") if calls(p, {"issue_proof"})]
    assert [p.name for p in minting] == ["policy_reader.py"]


def test_signing_keys_are_built_only_by_access_control_and_its_bootstrap() -> None:
    allowed = {"src/lda/bootstrap/access_control.py"}
    for path in py_files(SRC):
        if AC in path.parents:
            continue
        if calls(path, {"SigningKey", "SigningKeyring"}):
            assert rel(path) in allowed, rel(path)


def test_the_signing_key_slice_is_loaded_only_by_access_control_bootstrap() -> None:
    for path in py_files(SRC):
        if AC in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if "AccessControlSettings" in text or "proof_key" in text:
            assert rel(path) == "src/lda/bootstrap/access_control.py", rel(path)


# --- decisions D1/D2: no roles, no foreign persistence ------------------------------------------------------
def test_no_roles_groups_or_rbac_vocabulary_in_access_control() -> None:
    forbidden = re.compile(r"\b(roles?|groups?|rbac|inherit\w*|wildcard|deny_rule)\b", re.I)
    for layer in LAYERS:
        for path in py_files(AC / layer):
            if "migrations" in path.parts:  # `role` there is the database role, checked below
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
            names += [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
            names += [
                n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.ClassDef)
            ]
            names += [n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)]
            assert not [n for n in names if forbidden.search(n)], rel(path)
    migration = next((AC / "infrastructure/persistence/migrations").rglob("a0001*.py"))
    columns = re.findall(r"^\s+(\w+) (?:uuid|text|timestamptz|bigint)", migration.read_text(), re.M)
    assert not [c for c in columns if forbidden.search(c)]


def test_persistence_references_only_the_access_control_schema() -> None:
    text = "\n".join(p.read_text(encoding="utf-8") for p in py_files(AC / "infrastructure"))
    schemas = set(
        re.findall(r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE|SCHEMA|REFERENCES)\s+([a-z_]+)\.", text)
    )
    assert schemas <= {"access_control"}, schemas
    assert not re.search(r"\bidentity\.principals\b|ForeignKey|REFERENCES", text, re.I)


def test_access_control_never_reads_another_modules_persistence() -> None:
    for layer in LAYERS:
        for path in py_files(AC / layer):
            assert not [
                n
                for n in imports(path)
                if "identity.infrastructure" in n
                or "identity.domain" in n
                or "identity.application" in n
            ], rel(path)


def test_the_resource_registry_is_code_defined_not_a_table() -> None:
    migration = next((AC / "infrastructure/persistence/migrations").rglob("a0001*.py"))
    tables = re.findall(r"CREATE TABLE access_control\.(\w+)", migration.read_text())
    assert sorted(tables) == ["grants", "policy_epochs"]


# --- D8: no middleware --------------------------------------------------------------------------------------
def test_no_http_layer_or_middleware_was_invented() -> None:
    assert not (AC / "presentation").exists()
    for path in py_files(SRC):
        assert not re.search(r"middleware", path.name, re.I), rel(path)
        assert not [n for n in imports(path) if n.split(".")[0] in {"fastapi", "starlette"}] or (
            "presentation" in path.parts or "platform" in path.parts
        ), rel(path)


# --- the kernel stays opaque (ADR-034, D3) ---------------------------------------------------------------------
def test_authorization_semantics_stay_out_of_the_kernel() -> None:
    forbidden = re.compile(
        r"AccessScope|AuthorizationProof|ProofVerifier|policy_epoch|ResourceRef|GrantAccess|hmac",
        re.I,
    )
    for path in py_files(SRC / "shared/kernel"):
        assert forbidden.search(path.read_text(encoding="utf-8")) is None, rel(path)


# --- no weakening ---------------------------------------------------------------------------------------------------
def test_no_waivers_or_ignored_imports_were_added_for_access_control() -> None:
    exceptions = (ROOT / "architecture-exceptions.toml").read_text(encoding="utf-8")
    assert "[[exception]]" not in exceptions.replace("# [[exception]]", "")
    linter = (ROOT / ".importlinter").read_text(encoding="utf-8")
    assert "ignore_imports" not in linter
    assert "access_control-layers" in linter and "access_control-domain-pure" in linter


# --- declarations (ADR-034) ---------------------------------------------------------------------------------------------
def test_every_use_case_file_declares_markers_from_the_kernel() -> None:
    for layer in ("use_cases", "queries"):
        files = [p for p in (AC / "application" / layer).glob("*.py") if p.name != "__init__.py"]
        assert files
        for path in files:
            assert "from lda.shared.kernel.declarations import" in path.read_text(encoding="utf-8")


def test_only_the_module_unit_of_work_builds_the_repositories() -> None:
    for repo_class in ("SqlGrantRepository(", "SqlPolicyEpochRepository("):
        users = [
            p
            for p in py_files(AC)
            if repo_class in p.read_text(encoding="utf-8") and "tests" not in p.parts
        ]
        assert [p.name for p in users] == ["unit_of_work.py"], repo_class


def test_stdlib_is_all_the_domain_needs() -> None:
    for path in py_files(AC / "domain"):
        for name in imports(path):
            top = name.split(".")[0]
            assert top in {"lda", "__future__"} or top in sys.stdlib_module_names, (rel(path), name)


# --- principal-disable decision: live Identity check, no reverse edge ------------------------------------
def test_identity_does_not_depend_on_access_control() -> None:
    """The live status check goes Access Control -> Identity only; Identity never learns of it."""
    manifests, _ = load_manifests(ROOT)
    dag, _ = load_dag(ROOT)
    assert manifests["identity"].depends_on == ()
    assert dag["identity"] == ()
    for path in py_files(SRC / "modules/identity"):
        assert not [n for n in imports(path) if "access_control" in n], rel(path)


def test_the_verifier_reaches_identity_only_through_its_public_api() -> None:
    verifier = AC / "application/proof_verification.py"
    identity_imports = [n for n in imports(verifier) if n.startswith("lda.modules.identity")]
    assert identity_imports
    assert all(re.fullmatch(r"lda\.modules\.identity\.api(\.\w+)?", n) for n in identity_imports)
    # No direct persistence shortcut for the status check:
    text = verifier.read_text(encoding="utf-8")
    assert "identity.principals" not in text and "principal_repository" not in text.lower()


def test_the_verifier_is_given_the_identity_boundary_by_bootstrap() -> None:
    source = (SRC / "bootstrap/access_control.py").read_text(encoding="utf-8")
    assert "identity.get_principal" in source
    assert source.count("EpochCheckedProofVerifier(") == 1


def test_identity_never_bumps_the_policy_epoch() -> None:
    for path in py_files(SRC / "modules/identity"):
        assert "policy_epoch" not in path.read_text(encoding="utf-8"), rel(path)
