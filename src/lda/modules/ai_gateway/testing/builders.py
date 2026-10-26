"""Builders for consumers' tests. AG090 allows `ContextSegment` to be constructed only by the
modules that own context assembly and by this package, so other tests build segments here."""

from __future__ import annotations

from lda.modules.ai_gateway.application.dto import ContextSegment
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofResource


def build_segment(kind: str, resource_id: str, text: str) -> ContextSegment:
    return ContextSegment(ProofResource(kind, resource_id), Sensitive(text))
