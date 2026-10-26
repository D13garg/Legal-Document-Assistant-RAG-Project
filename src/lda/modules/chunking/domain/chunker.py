"""Deterministic chunking of structured text (ADR-038). Pure: no I/O, no clock, no randomness.

Rules, in order of precedence:
1. A chunk never exceeds `max_characters` and never splits a block except when the block alone is
   longer than that. A long block is cut at a sentence end, else at whitespace, else at a code
   point that does not separate a combining mark, joiner or virama from what it attaches to.
2. Blocks are packed greedily, in order, up to `max_characters`; no text is dropped (whitespace
   between the pieces of a cut block is the only thing omitted) and none is duplicated (no overlap).
3. A heading opens a new chunk once the current one holds `min_characters`, so that a section is
   not mixed with the tail of the previous one; a heading is never left alone at the end of a
   chunk while its content follows (it moves to the next chunk when both fit).
Every boundary is explicit: a chunk lists the exact spans (`Segment`) it was cut from.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from lda.modules.chunking.domain.chunk import SEPARATOR, Chunk, Segment
from lda.modules.chunking.domain.failures import LIMIT_CHUNKS, NO_CHUNKABLE_TEXT, ChunkingRejected
from lda.shared.kernel.errors import ValidationError

CHUNKER_SCHEMA = "chunk-1"
SECTION_LABEL_LIMIT = 200
MIN_CHARACTER_CEILING = 100
MAX_CHARACTER_CEILING = 100_000

SPACED_TERMINATORS = frozenset(
    ".!?;\u2026\u0964\u0965\u061f\u06d4"
)  # a sentence ends here when whitespace follows
UNSPACED_TERMINATORS = frozenset(
    "\u3002\uff01\uff1f\uff61"
)  # CJK full stops need no following space
JOINERS = frozenset("\u200c\u200d")
VIRAMA = 9  # canonical combining class of Indic viramas (they join the next consonant)


@dataclass(frozen=True, slots=True)
class ChunkingParameters:
    """`max_characters`: hard ceiling of a chunk's text. `min_characters`: how much a chunk must
    hold before a heading may open a new one. `max_chunks`: a limit, not a shaping rule."""

    max_characters: int
    min_characters: int
    max_chunks: int

    def __post_init__(self) -> None:
        if not MIN_CHARACTER_CEILING <= self.max_characters <= MAX_CHARACTER_CEILING:
            raise ValidationError("chunking_parameters_invalid")
        if not 1 <= self.min_characters <= self.max_characters // 2 or self.max_chunks < 1:
            raise ValidationError("chunking_parameters_invalid")

    @property
    def version(self) -> str:
        """Identifies the chunking behaviour: schema, the shaping parameters and the Unicode
        database (a different one can classify new characters differently). Recorded with every
        chunk set; a changed value never rewrites an existing set (ADR-038)."""
        return (
            f"{CHUNKER_SCHEMA}; max={self.max_characters}; min={self.min_characters}"
            f"; unicode={unicodedata.unidata_version}"
        )


@dataclass(frozen=True, slots=True)
class SourceBlock:
    """One block of extracted text in reading order, as this module sees it (no foreign type in
    the domain). `heading_level` is set exactly for headings."""

    text: str
    heading_level: int | None = None
    page: int | None = None


@dataclass(frozen=True, slots=True)
class _Atom:
    block_index: int
    start: int
    end: int
    text: str
    path: tuple[str, ...]
    page: int | None
    heading: bool


def chunk_blocks(blocks: Sequence[SourceBlock], params: ChunkingParameters) -> tuple[Chunk, ...]:
    """Raises `ChunkingRejected` when there is nothing to chunk or the limit is exceeded."""
    packer = _Packer(params)
    trail: list[tuple[int, str]] = []
    for index, block in enumerate(blocks):
        region = _trimmed(block.text)
        if region is None:
            continue  # whitespace-only: nothing to cite, nothing to index
        if block.heading_level is not None:
            while trail and trail[-1][0] >= block.heading_level:
                trail.pop()
            trail.append((block.heading_level, _label(block.text[region[0] : region[1]])))
        path = tuple(label for _, label in trail)
        for start, end in _pieces(block.text, region, params.max_characters):
            packer.add(
                _Atom(
                    index,
                    start,
                    end,
                    block.text[start:end],
                    path,
                    block.page,
                    block.heading_level is not None,
                )
            )
    return packer.finish()


class _Packer:
    def __init__(self, params: ChunkingParameters) -> None:
        self._params = params
        self._chunks: list[Chunk] = []
        self._current: list[_Atom] = []

    def add(self, atom: _Atom) -> None:
        if self._current:
            length = _joined_length(self._current)
            overflows = length + len(SEPARATOR) + len(atom.text) > self._params.max_characters
            opens_section = atom.heading and length >= self._params.min_characters
            if overflows or opens_section:
                kept, carried = _split_trailing_headings(self._current)
                if carried and _joined_length([*carried, atom]) > self._params.max_characters:
                    kept, carried = self._current, []  # both would not fit: keep the order
                self._emit(kept)
                self._current = carried
        self._current.append(atom)

    def finish(self) -> tuple[Chunk, ...]:
        if self._current:
            self._emit(self._current)
            self._current = []
        if not self._chunks:
            raise ChunkingRejected(NO_CHUNKABLE_TEXT)
        return tuple(self._chunks)

    def _emit(self, atoms: list[_Atom]) -> None:
        if len(self._chunks) >= self._params.max_chunks:
            raise ChunkingRejected(LIMIT_CHUNKS)
        pages = [a.page for a in atoms if a.page is not None]
        self._chunks.append(
            Chunk(
                ordinal=len(self._chunks),
                text=SEPARATOR.join(a.text for a in atoms),
                segments=tuple(Segment(a.block_index, a.start, a.end) for a in atoms),
                section_path=atoms[0].path,
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
            )
        )


def _joined_length(atoms: Sequence[_Atom]) -> int:
    return sum(len(a.text) for a in atoms) + len(SEPARATOR) * (len(atoms) - 1)


def _split_trailing_headings(atoms: list[_Atom]) -> tuple[list[_Atom], list[_Atom]]:
    """(kept, carried): the trailing headings go with the next content; one atom always stays."""
    split = len(atoms)
    while split > 1 and atoms[split - 1].heading:
        split -= 1
    return atoms[:split], atoms[split:]


def _trimmed(text: str) -> tuple[int, int] | None:
    stripped = text.strip()
    if not stripped:
        return None
    start = len(text) - len(text.lstrip())
    return start, start + len(stripped)


def _label(heading: str) -> str:
    if len(heading) <= SECTION_LABEL_LIMIT:
        return heading
    return heading[: _hard_cut(heading, 0, SECTION_LABEL_LIMIT, SECTION_LABEL_LIMIT // 2)]


def _pieces(text: str, region: tuple[int, int], maximum: int) -> Iterator[tuple[int, int]]:
    """The spans of one block, each at most `maximum` long, none starting/ending in whitespace."""
    pos, end = region
    while end - pos > maximum:
        cut = _cut(text, pos, maximum)
        yield pos, cut
        pos = cut
        while text[pos].isspace():  # a non-space exists before `end`, so this stops
            pos += 1
    yield pos, end


def _cut(text: str, pos: int, maximum: int) -> int:
    """Where to end the piece starting at `pos`; `pos + maximum` is inside the text and the cut is
    always after the middle of the window, so every piece makes progress."""
    limit = pos + maximum
    lower = pos + maximum // 2
    for i in range(limit, lower, -1):
        previous = text[i - 1]
        if previous in UNSPACED_TERMINATORS or (
            previous in SPACED_TERMINATORS and text[i].isspace()
        ):
            return i
    for i in range(limit, lower, -1):
        if text[i].isspace():
            while text[i - 1].isspace():
                i -= 1
            return i
    return _hard_cut(text, pos, limit, lower)


def _hard_cut(text: str, pos: int, limit: int, lower: int) -> int:
    """No sentence end and no whitespace in the window: cut at a code point boundary that does not
    separate a combining mark, a joiner or a virama from its neighbour. Not full UAX #29 grapheme
    segmentation (the standard library has none): see the limitations in ADR-038."""
    cut = limit
    while cut > lower and (_attaches_to_previous(text[cut]) or _binds_next(text[cut - 1])):
        cut -= 1
    return cut if cut > lower else limit


def _attaches_to_previous(char: str) -> bool:
    return char in JOINERS or unicodedata.category(char).startswith("M")


def _binds_next(char: str) -> bool:
    return char in JOINERS or unicodedata.combining(char) == VIRAMA
