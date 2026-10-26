from __future__ import annotations

import argparse
import subprocess
from collections.abc import Sequence
from pathlib import Path

from archguard import adr, apilock, policy, runner, scaffold
from archguard.manifest import module_dirs
from archguard.model import Violation


def _report(violations: Sequence[Violation]) -> int:
    for v in violations:
        print(v.render())
    print(f"\n{len(violations)} architecture violation(s)" if violations else "architecture: OK")
    return 1 if violations else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="archguard")
    p.add_argument("--root", type=Path, default=Path.cwd())
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    g = sub.add_parser("gen-importlinter")
    g.add_argument("--check", action="store_true")
    a = sub.add_parser("api-lock")
    a.add_argument("--write", action="store_true")
    d = sub.add_parser("adr-gate")
    d.add_argument("--base", default="origin/main")
    d.add_argument("--files", nargs="*")
    n = sub.add_parser("new-module")
    n.add_argument("name")
    n.add_argument("--presentation", action="store_true")
    n.add_argument("--summary", default="TODO")
    args = p.parse_args(argv)
    root: Path = args.root.resolve()

    if args.cmd == "check":
        return _report(runner.run_all(root))
    if args.cmd == "gen-importlinter":
        text = runner.generated_importlinter(root)
        target = root / policy.IMPORTLINTER_FILE
        if args.check:
            return 0 if target.exists() and target.read_text(encoding="utf-8") == text else 1
        target.write_text(text, encoding="utf-8")
        return 0
    if args.cmd == "api-lock":
        names = [m.name for m in module_dirs(root)]
        if args.write:
            apilock.write_api_locks(root, names)
            return 0
        return _report(apilock.check_api_locks(root, names))
    if args.cmd == "adr-gate":
        changed = args.files
        if changed is None:
            out = subprocess.run(  # noqa: S603
                ["git", "diff", "--name-only", f"{args.base}...HEAD"],  # noqa: S607
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            changed = out.stdout.split()
        return _report(adr.adr_gate(changed))
    if args.cmd == "new-module":
        scaffold.create_module(
            root, args.name, presentation=args.presentation, summary=args.summary
        )
        (root / policy.IMPORTLINTER_FILE).write_text(
            runner.generated_importlinter(root), encoding="utf-8"
        )
        print(f"created {policy.MODULES_ROOT}/{args.name}")
        return 0
    return 2
