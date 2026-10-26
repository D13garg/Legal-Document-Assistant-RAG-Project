from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str
    symbol: str = ""

    def render(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        sym = f" [{self.symbol}]" if self.symbol else ""
        return f"{self.rule} {loc}{sym}: {self.message}"
