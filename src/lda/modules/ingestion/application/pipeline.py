"""The ordered plan of a run (ADR-010 §5): the mandatory `scan` stage (ADR-036) comes first."""

from __future__ import annotations

from collections.abc import Sequence

from lda.modules.ingestion.application.ports import (
    ContentScanner,
    PipelineStage,
    ScanRequest,
    ScanVerdict,
    StageOutcome,
    StageQuarantined,
    StageRequest,
    StageRetryable,
    StageSucceeded,
)
from lda.modules.ingestion.domain.errors import InvalidRun
from lda.modules.ingestion.domain.run import valid_stage_name
from lda.shared.kernel.errors import ConfigurationError

SCAN_STAGE = "scan"
MALWARE_CODE = "malware_detected"
SCAN_ERROR_CODE = "scan_error"


class ScanStage:
    """Maps the scanner's verdict to a stage outcome. Only an exact `clean` lets the run go on."""

    name = SCAN_STAGE

    def __init__(self, scanner: ContentScanner) -> None:
        self._scanner = scanner

    def run(self, request: StageRequest) -> StageOutcome:
        verdict = self._scanner.scan(
            ScanRequest(request.tenant_id, request.document, request.version, request.run_id)
        )
        if verdict is ScanVerdict.CLEAN:
            return StageSucceeded()
        if verdict is ScanVerdict.INFECTED:
            return StageQuarantined(MALWARE_CODE)
        return StageRetryable(SCAN_ERROR_CODE)  # `error` and anything unexpected: fail closed


class Pipeline:
    def __init__(self, scanner: ContentScanner, stages: Sequence[PipelineStage]) -> None:
        if not stages:
            raise ConfigurationError("ingestion_pipeline_empty")
        names = [stage.name for stage in stages]
        try:
            for name in names:
                valid_stage_name(name)
        except InvalidRun:
            raise ConfigurationError("ingestion_pipeline_names_invalid") from None
        if len(set(names)) != len(names) or SCAN_STAGE in names:
            raise ConfigurationError("ingestion_pipeline_names_invalid")
        self._stages: tuple[PipelineStage, ...] = (ScanStage(scanner), *stages)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self._stages)

    def get(self, name: str) -> PipelineStage | None:
        return next((s for s in self._stages if s.name == name), None)
