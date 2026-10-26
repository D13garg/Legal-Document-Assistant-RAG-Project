"""D7 through the real configuration loader: no default model, and nothing invalid is accepted.

The settings-class tests prove the field is required; these prove the *deployment path* (an
environment with `LDA_GROUNDED_GENERATION__*` variables) fails closed when the model is absent or
malformed, and that the error names the field but never echoes a rejected value.
"""

from __future__ import annotations

import pytest

from lda.modules.grounded_generation.config import GroundedGenerationSettings
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError

PREFIX = "LDA_GROUNDED_GENERATION__"


def load(**fields: str) -> GroundedGenerationSettings:
    env = {f"{PREFIX}{name.upper()}": value for name, value in fields.items()}
    return ConfigurationLoader(env).load("grounded_generation", GroundedGenerationSettings)


def test_a_valid_model_loads_and_everything_else_defaults_conservatively() -> None:
    settings = load(model="fake-model:1")
    assert settings.model == "fake-model:1"
    assert (settings.template_id, settings.template_version, settings.temperature) == (
        "grounded_answer",
        1,
        0.0,
    )


def test_an_absent_model_fails_closed_naming_the_field() -> None:
    with pytest.raises(ConfigurationError) as caught:
        ConfigurationLoader({}).load("grounded_generation", GroundedGenerationSettings)
    assert "model" in caught.value.detail


def test_other_settings_alone_never_supply_a_model() -> None:
    with pytest.raises(ConfigurationError) as caught:
        load(max_sources="5", max_statements="5", temperature="0.2")
    assert "model" in caught.value.detail


def test_an_unrelated_or_misspelled_variable_does_not_become_the_model() -> None:
    env = {"LDA_GROUNDED_GENERATION_MODEL": "m-1", "LDA_MODEL": "m-1", "MODEL": "m-1"}
    with pytest.raises(ConfigurationError):
        ConfigurationLoader(env).load("grounded_generation", GroundedGenerationSettings)


INVALID = {
    "empty": "",
    "blank": "   ",
    "inner space": "bad model SECRET-MODEL-7731",
    "trailing newline": "model-1\n",
    "leading newline": "\nmodel-1",
    "leading dash": "-model",
    "leading dot": ".model",
    "too long": "m" * 129,
    "non-ascii": "mod\u00e8le-1 SECRET-MODEL-7732",
    "nul": "model\x00-1",
    "shell metachar": "model;rm",
    "quote": 'model"1',
}


@pytest.mark.parametrize("value", INVALID.values(), ids=INVALID.keys())
def test_an_invalid_model_is_refused_and_never_echoed(value: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load(model=value)
    assert "model" in caught.value.detail
    assert "SECRET" not in caught.value.detail and "SECRET" not in repr(caught.value)


@pytest.mark.parametrize("value", ["m", "fake-model:1", "gpt.4o/mini-2", "a:b/c.d_e-f", "m" * 128])
def test_the_documented_model_label_shape_is_accepted(value: str) -> None:
    assert load(model=value).model == value


@pytest.mark.parametrize(
    "fields",
    [
        {"max_sources": "0"},
        {"max_statements": "201"},
        {"min_quote_characters": "0"},
        {"temperature": "1.5"},
        {"template_id": "Bad-Id"},
    ],
)
def test_other_invalid_bounds_also_fail_closed_even_with_a_valid_model(
    fields: dict[str, str],
) -> None:
    with pytest.raises(ConfigurationError):
        load(model="fake-model:1", **fields)
