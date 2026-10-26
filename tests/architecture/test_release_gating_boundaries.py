"""Step 17 pins (ADR-041): where the configuration identity is computed, that no second hashing
implementation exists, that the binding cannot be bypassed and that the DAG is unchanged.

Syntax-aware static checks with known limits (no data flow; dynamic imports evade them); the
behavioural tests in `tests/platform/test_rag_configuration.py` complement them.
"""

from __future__ import annotations

import ast
from pathlib import Path

from archguard.manifest import load_dag, load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
RAG = SRC / "bootstrap/rag_configuration.py"


def tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_the_dag_and_dependencies_are_unchanged_by_step_17() -> None:
    dag, _ = load_dag(ROOT)
    manifests, _ = load_manifests(ROOT)
    assert set(dag["evaluation"]) == {"ai_gateway", "prompts"}
    assert list(manifests["evaluation"].depends_on) == []
    assert list(manifests["answering"].depends_on) == [
        d for d in manifests["answering"].depends_on if d != "citations"
    ]  # still no citations edge (ADR-040 Q1)


def test_only_the_evaluation_module_hashes_the_configuration() -> None:
    """One hashing implementation: the identity digest lives in `evaluation.domain`. The bootstrap
    mapping hashes only the approved template wording, never the configuration."""
    for path in SRC.rglob("*.py"):
        parts = path.relative_to(SRC).parts
        if "tests" in parts or "testing" in parts:
            continue
        source = path.read_text(encoding="utf-8")
        if '"rag-" +' in source or "'rag-' +" in source:
            assert parts[:3] == ("modules", "evaluation", "domain"), path
    literals = [
        n.value
        for n in ast.walk(tree(RAG))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert not [v for v in literals if v.startswith("rag-") and len(v) < 10]  # no id is built here


def test_the_mapping_has_no_ad_hoc_environment_access_and_hashes_only_the_template() -> None:
    source = RAG.read_text(encoding="utf-8")
    assert "environ" not in source and "getenv" not in source
    assert source.count("hashlib.sha256(") == 1 and "template.instructions" in source


def test_answering_takes_its_config_id_only_from_the_runtime_configuration() -> None:
    source = (SRC / "bootstrap/answering.py").read_text(encoding="utf-8")
    assert "settings.config_id" not in source and "runtime.config_id" in source
    limits_call = [
        n
        for n in ast.walk(tree(SRC / "bootstrap/answering.py"))
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "AnswerLimits"
    ]
    assert len(limits_call) == 1
    assert ast.unparse(limits_call[0].args[0]) == "runtime.config_id"


def test_the_answering_settings_keep_the_config_id_optional_and_shape_checked() -> None:
    source = (SRC / "modules/answering/config.py").read_text(encoding="utf-8")
    assert "config_id: str | None = Field(default=None" in source
    assert "^rag-[0-9a-f]{64}$" in source


def test_no_production_code_accepts_a_bare_config_id_for_a_release_decision() -> None:
    """The three gate use cases take an `EffectiveConfiguration`, and refuse anything else."""
    source = (SRC / "modules/evaluation/application/use_cases/release_gating.py").read_text(
        encoding="utf-8"
    )
    assert source.count("isinstance(effective, EffectiveConfiguration)") == 3
    assert "def execute(self, effective: EffectiveConfiguration)" in source


def test_the_release_tables_have_no_update_or_delete_grant_in_the_migration() -> None:
    migration = (
        SRC
        / "modules/evaluation/infrastructure/persistence/migrations/versions/v0001_evaluation.py"
    ).read_text(encoding="utf-8")
    grants = [line for line in migration.splitlines() if "GRANT" in line]
    assert grants and all("SELECT, INSERT" in g or "USAGE" in g for g in grants)
    assert "UPDATE ON" not in migration and "DELETE ON" not in migration
    assert "ROW LEVEL SECURITY" not in migration  # global synthetic data, by decision D3


def test_evaluation_still_imports_no_other_module() -> None:
    for path in (SRC / "modules/evaluation").rglob("*.py"):
        if {"tests", "testing"} & set(path.parts):
            continue
        for node in ast.walk(tree(path)):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                if parts[:2] == ["lda", "modules"]:
                    assert parts[2] == "evaluation", (path.name, node.module)
