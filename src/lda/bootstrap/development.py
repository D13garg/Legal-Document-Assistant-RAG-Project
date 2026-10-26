"""DEVELOPMENT-ONLY adapter set for the headless slice (ADR-045). None of this is production.

* The scanner here inspects NO bytes (the port carries identifiers only) and always answers
  CLEAN. It is not malware scanning and proves nothing about a file.
* The embedder is the one offline `DevelopmentEmbedder` the owner approved (ADR-007 amendment,
  ADR-045 D3 option (a)): a hashed bag of words. It is not semantic search.
* The LLM (bootstrap/ai_gateway.py, the one file allowed to name the provider port) is
  extractive: it copies one sentence the request itself supplied, so it cannot invent a statement,
  a quote or a citation. It is not a language model and makes no network call.

Every factory requires `require_development(config)`: `LDA_DEVELOPMENT__ENVIRONMENT` must equal
`development` (no default) and the database host must be loopback or a Unix socket. The guard is
accident prevention, not a defence against whoever controls the environment.
"""

from __future__ import annotations

from typing import Literal

from lda.bootstrap.ai_gateway import DEVELOPMENT_MODEL, DevelopmentLlm, build_development_provider
from lda.bootstrap.dev_operator import is_local_database
from lda.bootstrap.retrieval import Embedder, build_development_embedder
from lda.modules.ingestion.api import ContentScanner, ScanRequest, ScanVerdict
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.configuration.settings import DatabaseSettings, SettingsSlice
from lda.shared.kernel.errors import ConfigurationError

SECTION = "development"
ADAPTER_SET = "development"
__all__ = ["ADAPTER_SET", "DEVELOPMENT_MODEL", "require_development"]


class DevelopmentSettings(SettingsSlice):
    """`LDA_DEVELOPMENT__ENVIRONMENT`: required, no default, exactly `development`."""

    environment: Literal["development"]


def require_development(config: ConfigurationLoader) -> None:
    """Fail closed with a sanitized code; values are never echoed."""
    config.load(SECTION, DevelopmentSettings)
    if not is_local_database(config.load("database", DatabaseSettings).url.get_secret_value()):
        raise ConfigurationError(
            "development_requires_local_database",
            detail="development adapters refuse a non-loopback database host",
        )


class DevelopmentScanner:
    """Always CLEAN. Inspects nothing: the port carries identifiers, never bytes."""

    def scan(self, request: ScanRequest) -> ScanVerdict:
        return ScanVerdict.CLEAN


def development_scanner(config: ConfigurationLoader) -> ContentScanner:
    require_development(config)
    return DevelopmentScanner()


def development_embedder(config: ConfigurationLoader) -> Embedder:
    require_development(config)
    return build_development_embedder()


def development_llm(config: ConfigurationLoader) -> DevelopmentLlm:
    require_development(config)
    return build_development_provider()
