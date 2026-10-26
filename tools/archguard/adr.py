"""ADR lint (AG070), decisions-log consistency (AG071), ADR gate (AG072)."""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

from archguard import policy
from archguard.model import Violation

_FILE = re.compile(r"^(ADR-\d{3})-.+\.md$")
_ROW = re.compile(r"^\|\s*\[?(ADR-\d{3})\]?(?:\([^)]*\))?\s*\|[^|]*\|\s*([^|]+?)\s*\|")
_LOG_STATUS = re.compile(rf"^(Planned|{policy.ADR_STATUS_PATTERN})$")


def lint_adrs(root: Path) -> list[Violation]:
    adr_dir = root / policy.ADR_DIR
    if not adr_dir.exists():
        return []
    out: list[Violation] = []
    statuses: dict[str, str] = {}
    for f in sorted(adr_dir.glob("*.md")):
        rel = f.relative_to(root).as_posix()
        m = _FILE.match(f.name)
        if not m:
            out.append(Violation("AG070", rel, 0, "ADR files are named ADR-NNN-slug.md"))
            continue
        lines = f.read_text(encoding="utf-8").splitlines()
        if not lines or not re.match(rf"^# {m.group(1)}: \S", lines[0]):
            out.append(Violation("AG070", rel, 1, f"first line must be '# {m.group(1)}: <title>'"))
        headings = [ln[3:].strip() for ln in lines if ln.startswith("## ")]
        for h in policy.ADR_REQUIRED_HEADINGS:
            if h not in headings:
                out.append(Violation("AG070", rel, 0, f"missing section '## {h}'"))
        status = ""
        if "Status" in headings:
            idx = lines.index("## Status")
            status = next((ln.strip() for ln in lines[idx + 1 :] if ln.strip()), "")
            if not re.fullmatch(policy.ADR_STATUS_PATTERN, status):
                out.append(Violation("AG070", rel, idx + 1, f"invalid status '{status}'"))
        statuses[m.group(1)] = status
    out.extend(_check_log(root, statuses))
    return out


def _check_log(root: Path, statuses: dict[str, str]) -> list[Violation]:
    log = root / policy.DECISIONS_LOG
    if not log.exists():
        return [Violation("AG071", policy.DECISIONS_LOG, 0, "decisions-log.md is missing")]
    rows: dict[str, str] = {}
    out: list[Violation] = []
    for n, line in enumerate(log.read_text(encoding="utf-8").splitlines(), start=1):
        m = _ROW.match(line)
        if not m:
            continue
        if m.group(1) in rows:
            out.append(Violation("AG071", policy.DECISIONS_LOG, n, f"duplicate row {m.group(1)}"))
        rows[m.group(1)] = m.group(2)
        if not _LOG_STATUS.match(m.group(2)):
            out.append(
                Violation("AG071", policy.DECISIONS_LOG, n, f"invalid status '{m.group(2)}'")
            )
        elif m.group(2) != "Planned" and m.group(1) not in statuses:
            out.append(Violation("AG071", policy.DECISIONS_LOG, n, f"{m.group(1)} has no ADR file"))
    for adr_id, status in statuses.items():
        if adr_id not in rows:
            out.append(Violation("AG071", policy.DECISIONS_LOG, 0, f"{adr_id} is not indexed"))
        elif status and rows[adr_id] != status:
            out.append(
                Violation(
                    "AG071",
                    policy.DECISIONS_LOG,
                    0,
                    f"{adr_id} status '{rows[adr_id]}' != ADR file '{status}'",
                )
            )
    return out


def adr_gate(changed: list[str]) -> list[Violation]:
    governed = [f for f in changed if any(fnmatch(f, g) for g in policy.ADR_GOVERNED_GLOBS)]
    if not governed:
        return []
    adr_changed = any(fnmatch(f, f"{policy.ADR_DIR}/ADR-*.md") for f in changed)
    log_changed = policy.DECISIONS_LOG in changed
    if adr_changed and log_changed:
        return []
    return [
        Violation(
            "AG072",
            g,
            0,
            "architecture-governed change requires an added/modified ADR and an updated decisions-log.md",
        )
        for g in governed
    ]
