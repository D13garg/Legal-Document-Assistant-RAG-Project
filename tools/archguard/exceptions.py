"""Architecture exceptions with mandatory owner, ADR and expiry (AG060/AG061)."""

from __future__ import annotations

import datetime as dt
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from archguard import policy
from archguard.model import Violation

_REQUIRED = ("rule", "path", "owner", "adr", "expires", "reason")


@dataclass(frozen=True)
class Waiver:
    rule: str
    path: str
    symbol: str
    expires: dt.date


def load_waivers(root: Path, today: dt.date) -> tuple[list[Waiver], list[Violation]]:
    file = root / policy.EXCEPTIONS_FILE
    if not file.exists():
        return [], []
    data = tomllib.loads(file.read_text(encoding="utf-8"))
    waivers: list[Waiver] = []
    problems: list[Violation] = []
    for i, e in enumerate(data.get("exception", []), start=1):
        tag = f"exception #{i}"

        def bad(msg: str, tag: str = tag) -> None:
            problems.append(Violation("AG060", policy.EXCEPTIONS_FILE, 0, f"{tag}: {msg}"))

        missing = [k for k in _REQUIRED if not e.get(k)]
        if missing:
            bad(f"missing {', '.join(missing)}")
            continue
        if e["rule"] in policy.NON_WAIVABLE:
            bad(f"rule {e['rule']} can never be waived")
            continue
        adr = str(e["adr"])
        adr_dir = root / policy.ADR_DIR
        if not re.fullmatch(r"ADR-\d{3}", adr) or not list(adr_dir.glob(f"{adr}-*.md")):
            bad(f"adr '{adr}' does not exist")
            continue
        expires = e["expires"]
        if not isinstance(expires, dt.date):
            bad("expires must be a TOML date (YYYY-MM-DD)")
            continue
        if expires < today:
            bad(f"expired on {expires}; fix the violation or renew via ADR")
            continue
        waivers.append(Waiver(e["rule"], e["path"], e.get("symbol", ""), expires))
    return waivers, problems


def apply_waivers(violations: list[Violation], waivers: list[Waiver]) -> list[Violation]:
    used: set[Waiver] = set()
    kept: list[Violation] = []
    for v in violations:
        match = next(
            (
                w
                for w in waivers
                if w.rule == v.rule and w.path == v.path and (not w.symbol or w.symbol == v.symbol)
            ),
            None,
        )
        if match and v.rule not in policy.NON_WAIVABLE:
            used.add(match)
        else:
            kept.append(v)
    for w in waivers:
        if w not in used:
            kept.append(
                Violation(
                    "AG061",
                    policy.EXCEPTIONS_FILE,
                    0,
                    f"stale exception for {w.rule} {w.path}; remove it",
                )
            )
    return kept
