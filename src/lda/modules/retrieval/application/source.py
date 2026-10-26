"""Neutral shape of chunk content as retrieval consumes it. Text is content: `repr=False`."""

from __future__ import annotations

from dataclasses import dataclass, field

from lda.modules.retrieval.domain.ids import ChunkId


@dataclass(frozen=True, slots=True)
class SourceChunk:
    chunk_id: ChunkId
    ordinal: int
    text: str = field(repr=False)
    text_hash: str


@dataclass(frozen=True, slots=True)
class SourceChunks:
    chunker_version: str
    content_hash: str
    chunks: tuple[SourceChunk, ...] = field(repr=False)
