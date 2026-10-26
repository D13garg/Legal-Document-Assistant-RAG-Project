"""Evaluation bootstrap: settings load and one composition run over fakes (ADR-018, ADR-031).

Offline: no database, no provider, no network. The target is a test double supplied by the caller.
"""

from __future__ import annotations

import pytest

from lda.bootstrap.evaluation import build_evaluation, load_evaluation_settings
from lda.modules.evaluation.api import (
    ISOLATION,
    GatePolicy,
    MetricThreshold,
    Observation,
    Outcome,
    RunEvaluationCommand,
)
from lda.modules.evaluation.config import EvaluationSettings
from lda.modules.evaluation.testing import (
    PerfectTarget,
    ScriptedTarget,
    build_case,
    build_configuration,
    build_dataset,
)
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError, ValidationError


def test_settings_load_from_the_environment_with_the_slice_prefix() -> None:
    assert (
        load_evaluation_settings(ConfigurationLoader({"LDA_EVALUATION__MAX_CASES": "7"})).max_cases
        == 7
    )
    assert load_evaluation_settings(ConfigurationLoader({})) == EvaluationSettings()


def test_an_invalid_setting_is_a_configuration_error_without_the_value() -> None:
    loader = ConfigurationLoader({"LDA_EVALUATION__MAX_CASES": "not-a-number-SECRET"})
    with pytest.raises(ConfigurationError) as caught:
        load_evaluation_settings(loader)
    assert "SECRET" not in f"{caught.value!r} {caught.value.detail}"


def test_the_composed_runner_evaluates_and_gates() -> None:
    dataset = build_dataset(build_case("a", forbidden=("secret",)), build_case("b"))
    policy = GatePolicy((MetricThreshold(ISOLATION, 1.0),))
    command = RunEvaluationCommand(dataset, build_configuration(), policy)
    runner = build_evaluation(EvaluationSettings(), PerfectTarget()).run_evaluation
    assert runner.execute(command).decision.passed
    leaking = ScriptedTarget(
        {
            "a": Observation(Outcome.ANSWERED, ("src-1", "secret"), ("src-1",)),
            "b": Observation(Outcome.INSUFFICIENT_EVIDENCE),
        }
    )
    failing = build_evaluation(EvaluationSettings(), leaking).run_evaluation.execute(command)
    assert not failing.decision.passed


def test_the_configured_case_limit_is_enforced() -> None:
    runner = build_evaluation(EvaluationSettings(max_cases=1), PerfectTarget()).run_evaluation
    command = RunEvaluationCommand(
        build_dataset(build_case("a"), build_case("b")),
        build_configuration(),
        GatePolicy((MetricThreshold(ISOLATION, 1.0),)),
    )
    with pytest.raises(ValidationError):
        runner.execute(command)
