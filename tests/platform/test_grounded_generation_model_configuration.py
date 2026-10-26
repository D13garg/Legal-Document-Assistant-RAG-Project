"""D7 at the bootstrap entry point: `load_grounded_generation_settings` fails closed without a model."""

from __future__ import annotations

import pytest

from lda.bootstrap.grounded_generation import load_grounded_generation_settings
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError


def test_the_bootstrap_loader_refuses_a_deployment_without_a_model() -> None:
    with pytest.raises(ConfigurationError):
        load_grounded_generation_settings(ConfigurationLoader({}))


def test_the_bootstrap_loader_refuses_an_invalid_model_without_echoing_it() -> None:
    loader = ConfigurationLoader({"LDA_GROUNDED_GENERATION__MODEL": "bad model SECRET-8851"})
    with pytest.raises(ConfigurationError) as caught:
        load_grounded_generation_settings(loader)
    assert "SECRET" not in caught.value.detail


def test_the_bootstrap_loader_accepts_an_explicit_model() -> None:
    loader = ConfigurationLoader({"LDA_GROUNDED_GENERATION__MODEL": "fake-model:1"})
    assert load_grounded_generation_settings(loader).model == "fake-model:1"
