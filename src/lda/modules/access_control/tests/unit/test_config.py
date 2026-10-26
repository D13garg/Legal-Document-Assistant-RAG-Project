"""The access_control slice (ADR-031/ADR-026 §10): strong keys only, and never echoed."""

from __future__ import annotations

import base64

import pytest

from lda.modules.access_control.config import AccessControlSettings
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError


def key(n: int) -> str:
    return base64.urlsafe_b64encode(b"k" * n).decode().rstrip("=")


def load(**fields: str) -> AccessControlSettings:
    env = {f"LDA_ACCESS_CONTROL__{name.upper()}": value for name, value in fields.items()}
    return ConfigurationLoader(env).load("access_control", AccessControlSettings)


def test_a_valid_slice_loads_and_decodes_the_secret() -> None:
    settings = load(proof_key_id="primary-1", proof_key=key(32))
    assert settings.proof_key_id == "primary-1"
    assert settings.signing_secret() == b"k" * 32


def test_the_key_is_a_secret_and_never_in_repr() -> None:
    material = key(40)
    settings = load(proof_key_id="primary", proof_key=material)
    assert material not in repr(settings) and material not in str(settings)


@pytest.mark.parametrize("n", [0, 1, 16, 31])
def test_short_keys_are_rejected(n: int) -> None:
    with pytest.raises(ConfigurationError):
        load(proof_key_id="primary", proof_key=key(n) if n else "")


@pytest.mark.parametrize("bad", ["Primary", "", "-x", "has space", "x" * 33])
def test_bad_key_ids_are_rejected(bad: str) -> None:
    with pytest.raises(ConfigurationError):
        load(proof_key_id=bad, proof_key=key(32))


def test_missing_fields_and_unknown_fields_are_rejected() -> None:
    with pytest.raises(ConfigurationError):
        load(proof_key_id="primary")
    with pytest.raises(ConfigurationError):
        load(proof_key=key(32))
    with pytest.raises(ConfigurationError):
        load(proof_key_id="primary", proof_key=key(32), proof_ttl_seconds="3600")  # not tunable


def test_configuration_errors_never_echo_the_key() -> None:
    weak = key(5)
    with pytest.raises(ConfigurationError) as caught:
        load(proof_key_id="primary", proof_key=weak)
    error = caught.value
    assert weak not in str(error) + error.detail + repr(error)
    assert error.__cause__ is None and error.__context__ is None


def test_non_base64_keys_are_rejected_without_echo() -> None:
    with pytest.raises(ConfigurationError) as caught:
        load(proof_key_id="primary", proof_key="not base64 !!" * 5)
    assert "not base64" not in caught.value.detail
