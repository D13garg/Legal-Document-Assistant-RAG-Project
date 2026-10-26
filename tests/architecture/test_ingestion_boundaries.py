"""Step 6 pins: ingestion is an engine, not a parser; the queue contract carries a fencing token;
the approved decisions exist as Accepted ADRs; nothing speculative was introduced."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from archguard.manifest import load_dag, load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
ING = SRC / "modules/ingestion"
ADR = ROOT / "docs/architecture/ADR"


def py_files(base: Path) -> list[Path]:
    return [p for p in base.rglob("*.py") if "__pycache__" not in p.parts]


def production_files() -> list[Path]:
    return [p for p in py_files(ING) if "tests" not in p.parts and "migrations" not in p.parts]


def imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def defined_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.append(node.name)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
    return names


def test_ingestion_declares_no_dependency_and_the_dag_edge_points_into_it() -> None:
    manifests, _ = load_manifests(ROOT)
    dag, _ = load_dag(ROOT)
    assert manifests["ingestion"].depends_on == ()
    assert dag["ingestion"] == ()
    for consumer in ("documents", "document_processing", "chunking", "retrieval"):
        assert "ingestion" in dag[consumer]  # the PipelineStage inversion (ADR-010 §4)
    assert dict(manifests)["documents"].depends_on == ("ingestion",)


def test_ingestion_does_not_absorb_parsing_chunking_embedding_or_retrieval() -> None:
    banned = re.compile(
        r"pars(e|er|ing)\b|\bocr|normali[sz]|chunk|embed|retriev|rerank|generate|citation|evaluat"
        r"|tokeniz",
        re.IGNORECASE,
    )
    offenders = []
    for path in production_files():
        for name in defined_names(path):
            if banned.search(name):
                offenders.append((path.relative_to(ROOT).as_posix(), name))
    assert offenders == []


def test_no_speculative_queue_infrastructure_was_introduced() -> None:
    """Redis exists only as the rate limiter's adapter (Step 1); the job queue, events and the
    modules built on them stay on Postgres (ADR-012). No task framework or workflow engine at all."""
    anywhere = ("celery", "temporalio", "kombu", "dramatiq", "arq", "rq")
    queue_side = ("redis", *anywhere)
    queue_dirs = (
        SRC / "platform/database",
        SRC / "platform/events",
        SRC / "modules/ingestion",
        SRC / "modules/documents",
        SRC / "bootstrap",
    )
    for path in py_files(SRC):
        for name in imports(path):
            assert name.split(".")[0] not in anywhere, (path.relative_to(ROOT), name)
    for base in queue_dirs:
        for path in py_files(base):
            for name in imports(path):
                assert name.split(".")[0] not in queue_side, (path.relative_to(ROOT), name)


# ADR-011 kept `apps/` empty at Step 6. ADR-043 (Step 20A, owner-authorized) supersedes that with
# an explicit allow-list: nothing may appear under `apps/` unless it is named here.
ALLOWED_APP_ENTRY_POINTS = frozenset(
    {
        "apps/dev_bootstrap.py",  # ADR-043
        "apps/provision.py",  # ADR-044
        "apps/worker.py",  # ADR-044
        "apps/dev_slice.py",  # ADR-045
    }
)


def test_only_allow_listed_entry_points_exist() -> None:
    apps = ROOT / "src/lda/apps" if (ROOT / "src/lda/apps").exists() else ROOT / "apps"
    created = py_files(apps) if apps.exists() else []
    found = {Path(p).resolve().relative_to(ROOT.resolve()).as_posix() for p in created}
    assert found <= ALLOWED_APP_ENTRY_POINTS, sorted(found - ALLOWED_APP_ENTRY_POINTS)


def test_fakes_live_only_in_testing_and_production_code_never_imports_them() -> None:
    for path in production_files():
        if "testing" in path.parts:
            continue
        assert not any(".testing" in i for i in imports(path)), path
    fakes = ING / "testing/fakes.py"
    assert fakes.exists()
    for path in production_files():
        if path != fakes and "testing" not in path.parts:
            assert "ScriptedStage" not in path.read_text(encoding="utf-8"), path


def test_production_composition_rejects_an_empty_pipeline() -> None:
    from lda.modules.ingestion.application.pipeline import Pipeline
    from lda.modules.ingestion.testing import ScriptedScanner
    from lda.shared.kernel.errors import ConfigurationError

    try:
        Pipeline(ScriptedScanner(), [])
    except ConfigurationError as exc:
        assert exc.code == "ingestion_pipeline_empty"
    else:
        raise AssertionError("an empty pipeline must be rejected")


def test_the_queue_contract_carries_a_fencing_token_everywhere_it_matters() -> None:
    from lda.platform.database.jobs.fence import PostgresJobFence
    from lda.platform.database.jobs.leases import JobLeaseStore
    from lda.shared.ports.jobs import ClaimedJob, JobFence

    assert "fencing_token" in inspect.signature(ClaimedJob).parameters
    for operation in ("heartbeat", "complete", "retry", "fail"):
        params = inspect.signature(getattr(JobLeaseStore, operation)).parameters
        assert "fencing_token" in params, operation
        assert params["fencing_token"].default is inspect.Parameter.empty, operation  # required
    assert list(inspect.signature(JobFence.verify).parameters) == [
        "self",
        "job_id",
        "fencing_token",
    ]
    assert "verify" in dir(PostgresJobFence)


def test_every_ingestion_write_path_verifies_the_fence() -> None:
    """Each application module that commits ingestion state through a job must call the fence."""
    committing = {
        "execute_run.py": 3,  # begin, close/complete, record
        "run_watchdog.py": 2,  # dead-letter, redeliver
    }
    for name, minimum in committing.items():
        path = next(p for p in production_files() if p.name == name)
        text = path.read_text(encoding="utf-8")
        assert text.count("fence.verify(") >= (1 if name == "execute_run.py" else minimum), name
        assert text.count("uow.commit()") >= 1


def test_the_step_6_decisions_are_accepted_adrs_and_logged() -> None:
    log = (ROOT / "docs/architecture/decisions-log.md").read_text(encoding="utf-8")
    for adr in (
        "ADR-010-async-ingestion-pipeline.md",
        "ADR-011-worker-architecture.md",
        "ADR-036-ingestion-quarantine-contract.md",
    ):
        text = (ADR / adr).read_text(encoding="utf-8")
        assert re.search(r"^## Status\s+Accepted\s*$", text, re.MULTILINE), adr
        assert adr in log, adr
    for amended in ("ADR-012", "ADR-015", "ADR-020", "ADR-023"):
        path = next(ADR.glob(f"{amended}-*.md"))
        assert "## Amendment (Step 6" in path.read_text(encoding="utf-8"), amended
    assert re.search(r"\| ADR-010 .*Planned", log) is None
    assert re.search(r"\| ADR-011 .*Planned", log) is None
