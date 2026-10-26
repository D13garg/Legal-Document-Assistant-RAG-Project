"""Fakes for tests of this module and of modules that depend on it (never imported by production
code)."""

from lda.modules.ai_gateway.testing.builders import build_segment
from lda.modules.ai_gateway.testing.fakes import FakeLlmProvider, FakeProofVerifier

__all__ = ["FakeLlmProvider", "FakeProofVerifier", "build_segment"]
