"""The `access_control` configuration slice (ADR-031, ADR-026 §10)."""

from __future__ import annotations

import base64
import binascii

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

MIN_KEY_BYTES = 32


class AccessControlSettings(BaseModel):
    """`LDA_ACCESS_CONTROL__PROOF_KEY_ID` and `LDA_ACCESS_CONTROL__PROOF_KEY` (base64url)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proof_key_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    proof_key: SecretStr

    @field_validator("proof_key")
    @classmethod
    def _strong_enough(cls, value: SecretStr) -> SecretStr:
        # Fixed messages: configuration errors must never echo the value.
        if len(_decode(value.get_secret_value())) < MIN_KEY_BYTES:
            raise ValueError("proof_key must decode to at least 32 bytes")
        return value

    def signing_secret(self) -> bytes:
        return _decode(self.proof_key.get_secret_value())


def _decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("proof_key must be base64url") from None
