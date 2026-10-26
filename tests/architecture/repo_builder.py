from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from archguard import apilock, policy, runner, scaffold

DAG = """
[modules.a]
depends_on = []
[modules.b]
depends_on = ["a"]
[modules.c]
depends_on = []
"""


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, rel: str, content: str = "") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def module_file(self, module: str, rel: str, content: str) -> str:
        full = f"src/lda/modules/{module}/{rel}"
        self.write(full, content)
        return full

    def set_deps(self, module: str, deps: list[str]) -> None:
        path = self.root / f"src/lda/modules/{module}/module.toml"
        text = re.sub(
            r"depends_on = \[.*\]",
            "depends_on = [" + ", ".join(f'"{d}"' for d in deps) + "]",
            path.read_text(),
        )
        path.write_text(text)

    def regen(self) -> None:
        apilock.write_api_locks(
            self.root, [p.name for p in (self.root / policy.MODULES_ROOT).iterdir() if p.is_dir()]
        )
        (self.root / policy.IMPORTLINTER_FILE).write_text(runner.generated_importlinter(self.root))

    def regen_text(self) -> str:
        """Regenerate `.importlinter` and return its text."""
        self.regen()
        return (self.root / policy.IMPORTLINTER_FILE).read_text(encoding="utf-8")

    def violations(self) -> list[str]:
        return [v.rule for v in runner.run_all(self.root)]


MakeRepo = Callable[..., Repo]


def build_factory(tmp_path: Path) -> MakeRepo:
    def build(dag: str = DAG, modules: tuple[str, ...] = ("a", "b", "c")) -> Repo:
        repo = Repo(tmp_path)
        repo.write(policy.DAG_FILE, dag)
        scaffold.base_packages(tmp_path)
        for m in modules:
            scaffold.create_module(tmp_path, m)
        repo.regen()
        return repo

    return build
