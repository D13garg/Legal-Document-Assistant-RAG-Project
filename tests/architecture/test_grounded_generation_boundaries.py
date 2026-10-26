"""Step 14 (ADR-029): architecture and security-ordering pins for `grounded_generation`.

These read the source, so a refactor that reorders authorization, reads or egress, adds a second
provider path, or lets text leak into logging fails here even if a behavioural test still passes.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
GG = SRC / "modules/grounded_generation"


def production_files() -> list[Path]:
    return [p for p in GG.rglob("*.py") if not {"tests", "testing"} & set(p.relative_to(GG).parts)]


def tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def imported(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def method(path: Path, cls: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError((cls, name))


def call_lines(fn: ast.FunctionDef, attr: str) -> list[int]:
    return sorted(
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
    )


# ---- dependency contract --------------------------------------------------------------------


def test_the_manifest_declares_exactly_the_dag_edges_and_no_chunking() -> None:
    manifest = tomllib.loads((GG / "module.toml").read_text(encoding="utf-8"))
    declared = set(manifest["dependencies"]["depends_on"])
    dag = tomllib.loads((ROOT / "docs/architecture/module-dag.toml").read_text(encoding="utf-8"))
    assert declared == set(dag["modules"]["grounded_generation"]["depends_on"])
    assert declared == {"access_control", "ai_gateway", "prompts", "citations", "documents"}
    assert "chunking" not in declared and "retrieval" not in declared


def test_production_code_uses_other_modules_through_their_public_api_only() -> None:
    for path in production_files():
        for name in imported(path):
            parts = name.split(".")
            if name.startswith("lda.modules.") and parts[2] != "grounded_generation":
                assert len(parts) >= 4 and parts[3] == "api", (path.name, name)


def test_chunk_text_never_arrives_except_through_citations() -> None:
    for path in production_files():
        assert not any(n.startswith("lda.modules.chunking") for n in imported(path)), path.name
        assert not any(n.startswith("lda.modules.retrieval") for n in imported(path)), path.name


def test_the_domain_is_pure() -> None:
    allowed = ("lda.shared.kernel", "lda.modules.grounded_generation.domain")
    stdlib = {"__future__", "json", "re", "unicodedata", "dataclasses"}
    for path in (GG / "domain").glob("*.py"):
        for name in imported(path):
            root = name.split(".")[0]
            assert root in stdlib or name.startswith(allowed), (path.name, name)


def test_only_infrastructure_touches_sql_access_control_and_platform() -> None:
    for path in production_files():
        names = imported(path)
        in_infra = "infrastructure" in path.relative_to(GG).parts
        if not in_infra:
            assert not any(n.split(".")[0] == "sqlalchemy" for n in names), path.name
            assert not any(n.startswith("lda.platform") for n in names), path.name
            assert not any(n.startswith("lda.modules.access_control") for n in names), path.name


# ---- ordering: authorization precedes context construction ------------------------------------------------


def test_the_use_case_authorizes_and_builds_evidence_before_anything_else() -> None:
    execute = method(
        GG / "application/use_cases/generate_grounded_answer.py",
        "GenerateGroundedAnswer",
        "execute",
    )
    (assemble,) = call_lines(execute, "assemble")
    (draft,) = call_lines(execute, "draft")
    (begin,) = call_lines(execute, "begin")
    (commit,) = call_lines(execute, "commit")
    records = [
        n.lineno
        for n in ast.walk(execute)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "record"
        and isinstance(n.func.value, ast.Call)  # GenerationRecorder(...).record(...)
    ]
    assert len(records) == 1
    (record,) = records
    (confirm,) = call_lines(execute, "confirm_access")
    returned = [n.lineno for n in ast.walk(execute) if isinstance(n, ast.Return)]
    assert assemble < draft < begin < record < commit  # nothing shown before it is recorded
    assert commit < confirm < max(returned)  # and a last decision precedes the release


def test_the_assembler_asks_for_access_before_the_builder_can_read_anything() -> None:
    assemble = method(GG / "application/evidence_assembler.py", "EvidenceAssembler", "assemble")
    (authorize,) = call_lines(assemble, "authorize_read")
    (build,) = call_lines(assemble, "build")
    assert authorize < build


def builder_reader_argument(source: str) -> ast.expr:
    """The first argument of every `TextEvidenceBuilder(...)` call in `source`."""
    calls = [
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "TextEvidenceBuilder"
    ]
    assert len(calls) == 1
    return calls[0].args[0]


def is_wrapped(argument: ast.expr) -> bool:
    return (
        isinstance(argument, ast.Call)
        and getattr(argument.func, "id", "") == "ReadyVersionContent"
        and len(argument.args) == 2
    )


def test_bootstrap_always_wraps_the_chunk_text_reader_in_the_lifecycle_gate() -> None:
    """`ReadChunks` serves `pending` versions; only `ready` ones may reach a provider. The gate is
    applied inside `build_grounded_generation`, so a caller cannot hand in a raw reader."""
    source = (SRC / "bootstrap/grounded_generation.py").read_text(encoding="utf-8")
    assert is_wrapped(builder_reader_argument(source))
    assert not is_wrapped(builder_reader_argument("TextEvidenceBuilder(content_reader, v, l)"))


def test_the_gate_asks_documents_before_it_touches_the_text_store() -> None:
    read = method(GG / "infrastructure/ready_version_content.py", "ReadyVersionContent", "read")
    (state,) = call_lines(read, "execute")
    (inner,) = call_lines(read, "read")
    assert state < inner


def test_only_the_gate_and_bootstrap_name_the_gate_and_no_other_production_code_reads_text() -> (
    None
):
    for path in production_files():
        if path.name == "ready_version_content.py":
            continue
        assert "ReadChunksContent" not in path.read_text(encoding="utf-8"), path.name


def test_the_drafter_renders_then_reauthorizes_then_sends() -> None:
    draft = method(GG / "application/answer_drafter.py", "AnswerDrafter", "draft")
    (render,) = call_lines(draft, "_render")
    (authorize,) = call_lines(draft, "authorize_read")
    (send,) = call_lines(draft, "execute")
    assert render < authorize < send


# ---- single egress path, single prompt path --------------------------------------------------------------------


def _names_in(path: Path) -> set[str]:
    return {n.id for n in ast.walk(tree(path)) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree(path)) if isinstance(n, ast.Attribute)
    }


def test_only_the_drafter_builds_prompts_and_gateway_commands() -> None:
    for path in production_files():
        names = _names_in(path)
        if path.name == "answer_drafter.py":
            continue
        for forbidden in (
            "GenerateTextCommand",
            "ContextSegment",
            "PromptRequest",
            "PromptContextInput",
        ):
            assert forbidden not in names, (path.name, forbidden)
        if path.name not in {"__init__.py"}:
            assert "LlmProvider" not in names and "ProviderRequest" not in names, path.name


def test_no_production_file_logs_prints_or_swallows_errors() -> None:
    for path in production_files():
        module = tree(path)
        assert "logging" not in imported(path), path.name
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"print", "breakpoint", "eval", "exec"}, path.name
            if isinstance(node, ast.ExceptHandler):
                assert node.type is not None, path.name  # no bare except
                caught = ast.unparse(node.type)
                assert "Exception" not in caught and "BaseException" not in caught, path.name


def test_the_answer_is_built_only_after_validation_in_one_place() -> None:
    constructors = []
    for path in production_files():
        for node in ast.walk(tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AnswerStatement"
            ):
                constructors.append(path.name)
    assert constructors == ["generate_grounded_answer.py"]


# ---- the contract is internally consistent ---------------------------------------------------------------------------


def test_codes_are_namespaced_and_distinct() -> None:
    from lda.modules.grounded_generation.domain import codes

    values = [v for k, v in vars(codes).items() if k.isupper() and isinstance(v, str)]
    assert len(values) == len(set(values)) and all(v.startswith("grounded_") for v in values)
    assert set(values) >= codes.REJECTION_CODES


def test_the_structural_marker_equals_the_prompt_renderers() -> None:
    from lda.modules.grounded_generation.domain.answer_contract import STRUCTURAL_MARKER
    from lda.modules.prompts.domain.escaping import MARKER

    assert MARKER.lower() == STRUCTURAL_MARKER


def test_the_approved_template_states_the_contract_the_application_enforces() -> None:
    from lda.modules.grounded_generation.domain import answer_contract as contract
    from lda.modules.prompts.domain.catalog.proposed_grounded_answer import PROPOSED_GROUNDED_ANSWER

    wording = PROPOSED_GROUNDED_ANSWER.instructions
    for token in ('"outcome"', '"statements"', '"text"', '"citation"', '"quote"', "S2", "part="):
        assert token in wording, token
    assert contract.OUTCOME_ANSWERED in wording and contract.OUTCOME_INSUFFICIENT in wording
    assert contract.STRUCTURAL_MARKER not in wording.lower()
    assert PROPOSED_GROUNDED_ANSWER.test_only is False  # owner-approved 2026-10-16 (ADR-029 D8)


def test_the_production_catalog_serves_only_the_approved_grounded_template() -> None:
    from lda.modules.prompts.api import production_catalog
    from lda.modules.prompts.domain.catalog.proposed_grounded_answer import PROPOSED_GROUNDED_ANSWER

    assert production_catalog().get("grounded_answer", 1) is PROPOSED_GROUNDED_ANSWER
    for template_id, version in (("grounded_answer", 2), ("grounded_answer", 0), ("other", 1)):
        try:
            production_catalog().get(template_id, version)
        except Exception as refused:
            assert getattr(refused, "code", "") == "prompt_template_unknown"
        else:
            raise AssertionError("a template that was never approved is reachable in production")
