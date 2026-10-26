"""Fake stages and scanner for Step 6 (ADR-010 §5). They move no bytes and see identifiers only."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from lda.modules.ingestion.application.ports import (
    ScanRequest,
    ScanVerdict,
    StageOutcome,
    StageRequest,
    StageSucceeded,
)

Step = StageOutcome | BaseException | Callable[[StageRequest], StageOutcome]


class ScriptedStage:
    """Returns (or raises) its scripted steps in order, then `StageSucceeded` for ever."""

    def __init__(self, name: str, steps: Iterable[Step] = ()) -> None:
        self._name = name
        self._steps = list(steps)
        self.calls: list[StageRequest] = []

    @property
    def name(self) -> str:
        return self._name

    def run(self, request: StageRequest) -> StageOutcome:
        self.calls.append(request)
        if not self._steps:
            return StageSucceeded()
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(request)
        return step


class ScriptedScanner:
    """Returns its scripted verdicts in order, then `clean` for ever."""

    def __init__(self, verdicts: Iterable[ScanVerdict | BaseException] = ()) -> None:
        self._verdicts = list(verdicts)
        self.requests: list[ScanRequest] = []

    def scan(self, request: ScanRequest) -> ScanVerdict:
        self.requests.append(request)
        if not self._verdicts:
            return ScanVerdict.CLEAN
        verdict = self._verdicts.pop(0)
        if isinstance(verdict, BaseException):
            raise verdict
        return verdict
