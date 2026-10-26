"""Step 7 pins: what `document_processing` is allowed to touch and what it must never become
(ADR-037, ADR-010, ADR-036). These inspect the real repository, not fixtures.

Each rule is a small detector over source text. The detectors are themselves exercised against
synthetic violations below, so a rule cannot pass vacuously because its pattern stopped matching.

Scope notes:
* "Production" means everything under the module except `tests/`. The module's own tests may
  build fixtures with parser libraries; production code may not use them outside the parsers.
* Step 8 (ADR-038) widened this deliberately: the `chunking` module (its manifest declares the DAG
  edge) and its composition root may import `document_processing.api`; nothing else may.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

from archguard.manifest import load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
DP = SRC / "modules/document_processing"
DP_PACKAGE = "lda.modules.document_processing"
PARSERS = DP / "infrastructure/parsers"
DOCUMENTS_TABLES = SRC / "modules/documents/infrastructure/persistence/tables.py"

# Libraries that read or unpack document bytes. Only `infrastructure/parsers` may import them.
PARSER_ROOTS = frozenset(
    {
        "pypdf",
        "PyPDF2",
        "pdfminer",
        "pdfplumber",
        "pypdfium2",
        "fitz",
        "pymupdf",
        "docx",
        "defusedxml",
        "lxml",
        "xml",
        "zipfile",
        "zlib",
    }
)
# Logging, process spawning and OCR: none belongs in this module (ADR-037). Document text must not
# reach a log line, and extraction is in-process with no OCR (a scan is `no_extractable_text`).
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "logging",
        "subprocess",
        "multiprocessing",
        "pytesseract",
        "tesserocr",
        "easyocr",
        "ocrmypdf",
        "paddleocr",
        "doctr",
    }
)
FORBIDDEN_CALLS = frozenset(
    {"print", "breakpoint", "getLogger", "system", "popen", "fork", "Popen", "check_output"}
)
# `ocr` as a word: also inside snake_case (`run_ocr_pass`) and camelCase (`runOcrPass`), but not
# inside an ordinary word such as `docroot`.
OCR_WORD = re.compile(
    r"(?<![A-Za-z])[Oo][Cc][Rr](?![a-z])|[a-z]Ocr(?![a-z])|[a-z]OCR(?![A-Za-z])|(?i:tesseract)"
)
EMIT_CALLS = frozenset({"publish", "enqueue", "emit"})
# Processing can only report a stage outcome; only Documents' own handler readies a version.
OUTCOME_NAMES = frozenset({"RecordVersionOutcome", "RecordOutcomeCommand"})
# The only places outside the module that may import it: composition roots, and (Step 8, ADR-038)
# the one module whose manifest declares the edge.
PERMITTED_IMPORTERS = frozenset(
    {"src/lda/bootstrap/processing.py", "src/lda/bootstrap/chunking.py"}
)
PERMITTED_IMPORTER_MODULE = "src/lda/modules/chunking/"
# What production code may take from Documents' public API (ADR-037 decision 1; Step 8 adds the
# lifecycle check `ReadVersionState` and its query type, ADR-038 §1).
DOCUMENTS_API_ALLOWED = frozenset(
    {
        "DocumentId",
        "DocumentVersionId",
        "DocumentPurged",
        "ReadOriginal",
        "ReadOriginalCommand",
        "ORIGINAL_MISSING",
        "ReadVersionState",
        "VersionStateQuery",
        "VersionStatus",
    }
)


# ---- helpers -----------------------------------------------------------------------------------


def py_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def production_files() -> list[Path]:
    return [p for p in py_files(DP) if "tests" not in p.parts]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def import_targets(source: str) -> list[str]:
    """Every dotted name an import statement can bind: `from a.b import c` yields a.b and a.b.c."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
            out += [f"{node.module}.{a.name}" for a in node.names]
    return out


def imported_names_from(source: str, module: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names |= {a.name for a in node.names}
    return names


def code_only(source: str) -> str:
    """The source's tokens minus comments and triple-quoted strings (docstrings). Tokenizing,
    unlike a regex, cannot let a `#` swallow the rest of the file."""
    kept: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and token.string.lstrip("rbfuRBFU")[:3] in ('"""', "'''"):
            continue
        kept.append(token.string)
    return " ".join(kept)


def parser_library_imports(source: str) -> set[str]:
    return {t.split(".")[0] for t in import_targets(source)} & PARSER_ROOTS


def forbidden_import_or_call(source: str) -> set[str]:
    found = {t.split(".")[0] for t in import_targets(source)} & FORBIDDEN_IMPORT_ROOTS
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else ""
            )
            if name in FORBIDDEN_CALLS:
                found.add(f"{name}()")
    if OCR_WORD.search(code_only(source)):
        found.add("ocr")
    return found


def event_patterns(source: str) -> set[str]:
    """Subclassing an event type or calling an emit method. Consuming an event is allowed."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = (
                    base.id
                    if isinstance(base, ast.Name)
                    else base.attr
                    if isinstance(base, ast.Attribute)
                    else ""
                )
                if base_name.endswith("Event"):
                    found.add(f"class {node.name}({base_name})")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in EMIT_CALLS
        ):
            found.add(f".{node.func.attr}()")
    return found


def outcome_usage(source: str) -> set[str]:
    """Any identifier, import alias, attribute or string that names an outcome use case or DTO."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in OUTCOME_NAMES:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in OUTCOME_NAMES:
            found.add(node.attr)
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in OUTCOME_NAMES:
            found.add(node.name.split(".")[-1])
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in OUTCOME_NAMES:
                found.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found |= {n for n in OUTCOME_NAMES if node.value == n}
    if any("record_version_outcome" in t for t in import_targets(source)):
        found.add("record_version_outcome module")
    return found


def imports_document_processing(source: str) -> bool:
    if any(t == DP_PACKAGE or t.startswith(DP_PACKAGE + ".") for t in import_targets(source)):
        return True
    return any(  # dynamic import by name
        isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and (n.value == DP_PACKAGE or n.value.startswith(DP_PACKAGE + "."))
        for n in ast.walk(ast.parse(source))
    )


def documents_table_names() -> set[str]:
    """Tables Documents owns, read from its own metadata so the rule follows the real schema."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(DOCUMENTS_TABLES.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Table":
            schema = [
                k.value
                for k in node.keywords
                if k.arg == "schema" and isinstance(k.value, ast.Constant)
            ]
            first = node.args[0] if node.args else None
            if schema and schema[0].value == "documents" and isinstance(first, ast.Constant):
                names.add(str(first.value))
    return names


def documents_table_references(source: str, tables: set[str]) -> set[str]:
    """Raw-text scan (SQL strings and comments included): schema-qualified names, the schema
    keyword, foreign keys into Documents, and Documents' persistence modules."""
    found: set[str] = set()
    alternatives = "|".join(sorted(re.escape(t) for t in tables))
    found |= {
        f"documents.{m}"
        for m in re.findall(rf"(?<!modules\.)\bdocuments\.({alternatives})\b", source)
    }
    if re.search(r"""schema\s*=\s*[rb]?["']documents["']""", source):
        found.add('schema="documents"')
    if re.search(r"\bREFERENCES\s+\"?documents\"?\.", source, re.IGNORECASE):
        found.add("REFERENCES documents.*")
    if any(t.startswith("lda.modules.documents.infrastructure") for t in import_targets(source)):
        found.add("documents infrastructure import")
    return found


# ---- the detectors detect (guards against vacuous rules) ---------------------------------------


def test_the_detectors_catch_synthetic_violations() -> None:
    assert parser_library_imports("import pypdf\nfrom defusedxml import ElementTree\n") == {
        "pypdf",
        "defusedxml",
    }
    assert parser_library_imports("from zipfile import ZipFile\n") == {"zipfile"}
    assert parser_library_imports("import hashlib\n") == set()

    assert forbidden_import_or_call("import logging\n") == {"logging"}
    assert forbidden_import_or_call("import subprocess\nsubprocess.run(['x'])\n") == {"subprocess"}
    assert forbidden_import_or_call("import os\nos.system('x')\n") == {"system()"}
    assert forbidden_import_or_call("print(text)\n") == {"print()"}
    assert forbidden_import_or_call("import pytesseract\n") == {"pytesseract", "ocr"}
    assert forbidden_import_or_call("def run_ocr_pass(): ...\n") == {"ocr"}
    assert forbidden_import_or_call("def runOcrPass(): ...\n") == {"ocr"}
    assert forbidden_import_or_call("docroot = 1\n") == set()


def test_a_comment_never_hides_a_later_ocr_identifier() -> None:
    """Regression: `code_only` once stripped `#.*` with DOTALL, so a comment deleted the rest of
    the file and every identifier after it escaped the OCR scan."""
    assert forbidden_import_or_call("# note\ndef run_ocr_pass(): ...\n") == {"ocr"}
    assert forbidden_import_or_call("x = 1  # trailing\nrun_ocr_pass()\n") == {"ocr"}
    assert forbidden_import_or_call('"""doc"""\n# note\nrun_ocr_pass()\n') == {"ocr"}
    # words inside comments and triple-quoted docstrings are still not code
    assert forbidden_import_or_call("# uses OCR later\nx = 1\n") == set()
    assert forbidden_import_or_call('def f():\n    """No OCR."""\n    return 1\n') == set()
    assert forbidden_import_or_call('"""No OCR here."""\nx = 1\n') == set()

    assert event_patterns("class Extracted(DomainEvent):\n    pass\n") == {
        "class Extracted(DomainEvent)"
    }
    assert event_patterns("class X(events.DomainEvent):\n    pass\n") == {"class X(DomainEvent)"}
    assert event_patterns("outbox.publish(e, d)\n") == {".publish()"}
    assert event_patterns("isinstance(event, DocumentPurged)\n") == set()  # consuming is fine

    assert outcome_usage("from lda.modules.documents.api import RecordVersionOutcome\n") == {
        "RecordVersionOutcome"
    }
    assert outcome_usage("x = api.RecordOutcomeCommand\n") == {"RecordOutcomeCommand"}
    assert outcome_usage(
        "import lda.modules.documents.application.use_cases.record_version_outcome\n"
    )
    assert outcome_usage('"""Never calls RecordVersionOutcome."""\nx = 1\n') == set()

    assert imports_document_processing("from lda.modules.document_processing.api import X\n")
    assert imports_document_processing("import lda.modules.document_processing\n")
    assert imports_document_processing("from lda.modules import document_processing\n")
    assert imports_document_processing(
        "importlib.import_module('lda.modules.document_processing')\n"
    )
    assert not imports_document_processing("from lda.modules.documents.api import X\n")

    tables = {"documents", "versions", "upload_intents", "purge_receipts"}
    assert documents_table_references('q = "SELECT 1 FROM documents.versions"', tables) == {
        "documents.versions"
    }
    assert documents_table_references('Table("t", m, schema="documents")', tables) == {
        'schema="documents"'
    }
    assert documents_table_references('q = "x REFERENCES documents.documents (a)"', tables) == {
        "documents.documents",
        "REFERENCES documents.*",
    }
    assert documents_table_references(
        "from lda.modules.documents.infrastructure.persistence import tables", tables
    ) == {"documents infrastructure import"}
    assert documents_table_references("from lda.modules.documents.api import X", tables) == set()
    assert documents_table_references("document_processing.extractions", tables) == set()


# ---- parsers are confined ----------------------------------------------------------------------


def test_parser_libraries_are_imported_only_under_infrastructure_parsers() -> None:
    offenders = {}
    for path in production_files():
        if PARSERS in path.parents:
            continue
        found = parser_library_imports(path.read_text(encoding="utf-8"))
        if found:
            offenders[rel(path)] = sorted(found)
    assert offenders == {}


def test_the_parsers_directory_really_holds_the_parser_libraries() -> None:
    """Non-vacuity: the confinement rule above has something to confine."""
    used: set[str] = set()
    for path in py_files(PARSERS):
        used |= parser_library_imports(path.read_text(encoding="utf-8"))
    assert {"pypdf", "defusedxml", "zipfile"} <= used


def test_domain_and_application_never_import_infrastructure() -> None:
    for layer in ("domain", "application"):
        for path in py_files(DP / layer):
            for target in import_targets(path.read_text(encoding="utf-8")):
                assert f"{DP_PACKAGE}.infrastructure" not in target, (rel(path), target)


# ---- prohibited patterns -----------------------------------------------------------------------


def test_no_logging_subprocess_ocr_or_print_in_production_code() -> None:
    offenders = {}
    for path in production_files():
        found = forbidden_import_or_call(path.read_text(encoding="utf-8"))
        if found:
            offenders[rel(path)] = sorted(found)
    assert offenders == {}


def test_the_module_consumes_events_but_never_emits_or_defines_one() -> None:
    """ADR-037: no event, no job, no audit fact. Its only event contact is the purge handler."""
    offenders = {}
    for path in production_files():
        found = event_patterns(path.read_text(encoding="utf-8"))
        if found:
            offenders[rel(path)] = sorted(found)
    assert offenders == {}


def test_the_purge_handler_still_subscribes_to_document_purged() -> None:
    """Non-vacuity for the event rule: the consuming side is real, and it is the only one."""
    handler = (DP / "application/event_handlers.py").read_text(encoding="utf-8")
    assert "DocumentPurged" in imported_names_from(handler, "lda.modules.documents.api")
    consumed = {
        name
        for path in production_files()
        for name in imported_names_from(
            path.read_text(encoding="utf-8"), "lda.modules.documents.api"
        )
        if name.startswith("Document") and name.endswith(("Purged", "Deleted", "Created"))
    }
    assert consumed == {"DocumentPurged"}


# ---- processing can never ready a version ------------------------------------------------------


def test_record_version_outcome_is_not_used_by_production_code_or_wiring() -> None:
    """Only Documents' own handler turns an ingestion outcome into a version status. Processing
    reports a `StageOutcome` to the engine; it can never mark a version ready or failed itself."""
    files = [*production_files(), ROOT / "src/lda/bootstrap/processing.py"]
    offenders = {}
    for path in files:
        found = outcome_usage(path.read_text(encoding="utf-8"))
        if found:
            offenders[rel(path)] = sorted(found)
    assert offenders == {}


def test_record_version_outcome_in_tests_is_confined_to_the_one_rig() -> None:
    """The module's tests arrange Documents' state (for example a version that is no longer
    `pending`) through `Rig.mark` in `tests/support.py`. That single harness is the only test-side
    use; it must not spread into individual tests, which would blur the production rule above."""
    users = {
        rel(p)
        for p in py_files(DP)
        if "tests" in p.parts and outcome_usage(p.read_text(encoding="utf-8"))
    }
    assert users == {"src/lda/modules/document_processing/tests/support.py"}


def test_production_code_takes_only_the_approved_names_from_documents_api() -> None:
    taken: set[str] = set()
    for path in production_files():
        if "testing" in path.parts:
            continue
        taken |= imported_names_from(path.read_text(encoding="utf-8"), "lda.modules.documents.api")
    assert taken <= DOCUMENTS_API_ALLOWED, sorted(taken - DOCUMENTS_API_ALLOWED)
    assert {"ReadOriginal", "ReadOriginalCommand", "DocumentPurged", "ReadVersionState"} <= taken


# ---- only the composition root imports the module ----------------------------------------------


def test_only_the_bootstrap_composition_root_imports_document_processing() -> None:
    importers = {
        rel(p)
        for p in py_files(SRC)
        if DP not in p.parents and imports_document_processing(p.read_text(encoding="utf-8"))
    }
    outside_chunking = {i for i in importers if not i.startswith(PERMITTED_IMPORTER_MODULE)}
    assert outside_chunking == PERMITTED_IMPORTERS
    assert any(i.startswith(PERMITTED_IMPORTER_MODULE) for i in importers)  # the edge is used


def test_no_other_manifest_depends_on_document_processing_yet() -> None:
    manifests, _ = load_manifests(ROOT)
    dependants = {
        name for name, manifest in manifests.items() if "document_processing" in manifest.depends_on
    }
    assert dependants == {"chunking"}  # Step 8 (ADR-038): the only dependant, as the DAG allows


# ---- Documents' tables are Documents' alone ----------------------------------------------------


def test_documents_owns_the_tables_this_rule_protects() -> None:
    assert documents_table_names() == {"documents", "versions", "upload_intents", "purge_receipts"}


def test_the_module_never_references_documents_tables() -> None:
    """No SQL, SQLAlchemy `schema=`, foreign key or persistence import reaches into Documents'
    schema. The module reads originals only through `documents.api.ReadOriginal`. Scans raw text
    (migrations, SQL strings and tests included), so a reference cannot hide in a comment."""
    tables = documents_table_names()
    offenders = {}
    for path in [*py_files(DP), ROOT / "src/lda/bootstrap/processing.py"]:
        found = documents_table_references(path.read_text(encoding="utf-8"), tables)
        if found:
            offenders[rel(path)] = sorted(found)
    assert offenders == {}


def test_the_migration_references_only_its_own_schema() -> None:
    migration = next(DP.glob("infrastructure/persistence/migrations/versions/e0001_*.py"))
    text = migration.read_text(encoding="utf-8")
    assert "document_processing." in text  # non-vacuity: it creates its own tables
    for target in re.findall(r"REFERENCES\s+([\w\".]+)", text, re.IGNORECASE):
        assert target.strip('"').startswith("document_processing."), target
