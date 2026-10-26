"""Deterministic canonical form and SHA-256 of an egress request (ADR-006).

The digest is content-derived, so it is evidence for provenance, never a public identifier:
a short low-entropy prompt could be guessed from it. It is not placed in logs or errors.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from lda.modules.ai_gateway.domain.parameters import GenerationParameters

CANONICAL_VERSION = "ai_gateway.request.v1"


def canonical_request(
    *,
    provider_id: str,
    model: str,
    parameters: GenerationParameters,
    instructions: str,
    segments: Sequence[tuple[str, str, str]],
) -> bytes:
    """Sorted keys, compact separators, ASCII-escaped JSON. Segment ORDER is meaningful (it is the
    order the provider sees) and is preserved; each segment is `(kind, id, text)`."""
    document = {
        "v": CANONICAL_VERSION,
        "provider": provider_id,
        "model": model,
        "parameters": {
            "max_output_tokens": parameters.max_output_tokens,
            "temperature": parameters.temperature,
        },
        "instructions": instructions,
        "segments": [{"kind": k, "id": i, "text": t} for k, i, t in segments],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def request_sha256(
    *,
    provider_id: str,
    model: str,
    parameters: GenerationParameters,
    instructions: str,
    segments: Sequence[tuple[str, str, str]],
) -> str:
    return hashlib.sha256(
        canonical_request(
            provider_id=provider_id,
            model=model,
            parameters=parameters,
            instructions=instructions,
            segments=segments,
        )
    ).hexdigest()
