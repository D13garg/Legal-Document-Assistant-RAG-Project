from archguard import runner
from repo_builder import MakeRepo


def test_fresh_scaffold_is_clean(make_repo: MakeRepo) -> None:
    assert make_repo().violations() == []


def test_unknown_module_directory_is_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file("a", "services/x.py", "")
    repo.module_file("a", "services/__init__.py", "")
    assert "AG001" in repo.violations()


def test_missing_readme_is_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    (repo.root / "src/lda/modules/a/README.md").unlink()
    assert "AG001" in repo.violations()


def test_non_empty_module_init_is_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file("a", "__init__.py", "from lda.modules.a.api import x\n")
    assert "AG001" in repo.violations()


def test_layers_must_match_directories(make_repo: MakeRepo) -> None:
    repo = make_repo()
    (repo.root / "src/lda/modules/a/presentation").mkdir()
    (repo.root / "src/lda/modules/a/presentation/__init__.py").touch()
    assert "AG001" in repo.violations()


def test_manifest_name_must_match_directory(make_repo: MakeRepo) -> None:
    repo = make_repo()
    p = repo.root / "src/lda/modules/a/module.toml"
    p.write_text(p.read_text().replace('name = "a"', 'name = "zzz"'))
    assert "AG002" in repo.violations()


def test_schema_must_equal_module_name(make_repo: MakeRepo) -> None:
    repo = make_repo()
    p = repo.root / "src/lda/modules/a/module.toml"
    p.write_text(p.read_text().replace('owns_schema = "a"', 'owns_schema = "shared"'))
    assert "AG002" in repo.violations()


def test_unapproved_context_is_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo(modules=("a",))
    from archguard import scaffold

    repo.write("docs/architecture/module-dag.toml", repo_dag_with_extra())
    scaffold.create_module(repo.root, "extra")
    repo.regen()
    repo.write("docs/architecture/module-dag.toml", "[modules.a]\ndepends_on = []\n")
    assert "AG003" in repo.violations()


def repo_dag_with_extra() -> str:
    return "[modules.a]\ndepends_on = []\n[modules.extra]\ndepends_on = []\n"


def test_edge_outside_approved_dag_is_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.set_deps("a", ["c"])  # approved DAG: a has no dependencies
    repo.module_file("a", "application/x.py", "from lda.modules.c.api import thing\n")
    assert "AG003" in repo.violations()


def test_cyclic_dag_file_is_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo(
        dag="[modules.a]\ndepends_on=['b']\n[modules.b]\ndepends_on=['a']\n", modules=("a", "b")
    )
    assert "AG003" in repo.violations()


def test_stale_importlinter_is_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    (repo.root / ".importlinter").write_text("# tampered\n")
    assert "AG004" in repo.violations()


def test_generated_config_contains_dag_order_and_layers(make_repo: MakeRepo) -> None:
    text = runner.generated_importlinter(make_repo().root)
    assert "a-layers" in text and "module-dag-order" in text
    # dependents above dependencies: b (depends on a) precedes a
    assert text.index("lda.modules.b\n") < text.index(
        "lda.modules.a\n", text.index("module-dag-order")
    )


def test_stale_pycache_inside_a_module_is_not_a_violation(make_repo: MakeRepo) -> None:
    repo = make_repo()
    (repo.root / "src/lda/modules/a/__pycache__").mkdir()
    assert "AG001" not in repo.violations()
