"""Test-only builders and fakes for evaluation (never imported by production code)."""

from lda.modules.evaluation.testing.builders import (
    PerfectTarget,
    ScriptedTarget,
    build_case,
    build_configuration,
    build_dataset,
    default_limits,
)
from lda.modules.evaluation.testing.release import InMemoryEvaluationStore, build_effective

__all__ = [
    "InMemoryEvaluationStore",
    "PerfectTarget",
    "ScriptedTarget",
    "build_case",
    "build_configuration",
    "build_dataset",
    "build_effective",
    "default_limits",
]
