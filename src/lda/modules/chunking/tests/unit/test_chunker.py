"""The chunker: determinism, boundaries, Unicode, oversized and degenerate input."""

from __future__ import annotations

import random
import unicodedata

import pytest

from lda.modules.chunking.domain.chunk import SEPARATOR
from lda.modules.chunking.domain.chunker import (
    SECTION_LABEL_LIMIT,
    ChunkingParameters,
    SourceBlock,
    chunk_blocks,
)
from lda.modules.chunking.domain.failures import LIMIT_CHUNKS, NO_CHUNKABLE_TEXT, ChunkingRejected
from lda.shared.kernel.errors import ValidationError

P = ChunkingParameters(max_characters=100, min_characters=20, max_chunks=1000)


def para(text: str, page: int | None = None) -> SourceBlock:
    return SourceBlock(text, None, page)


def head(text: str, level: int = 1) -> SourceBlock:
    return SourceBlock(text, level)


def rebuild(blocks: list[SourceBlock], chunk_segments: object) -> str:
    return SEPARATOR.join(blocks[s.block_index].text[s.start : s.end] for s in chunk_segments)  # type: ignore[attr-defined]


def squash(text: str) -> str:
    return "".join(text.split())


def assert_sound(blocks: list[SourceBlock], params: ChunkingParameters = P) -> None:
    chunks = chunk_blocks(blocks, params)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert 0 < len(chunk.text) <= params.max_characters
        assert chunk.text == rebuild(blocks, chunk.segments)  # boundaries reproduce the text
        assert chunk.text == chunk.text.strip()
    # nothing lost, nothing duplicated, order kept (whitespace between cut pieces aside)
    assert squash("".join(c.text for c in chunks)) == squash("".join(b.text for b in blocks))


# ---- parameters ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("maximum", "minimum", "limit"),
    [(99, 10, 5), (100_001, 10, 5), (100, 0, 5), (100, 51, 5), (100, 10, 0)],
)
def test_unsafe_parameters_are_refused(maximum: int, minimum: int, limit: int) -> None:
    with pytest.raises(ValidationError):
        ChunkingParameters(maximum, minimum, limit)


def test_the_version_names_every_shaping_parameter_and_the_unicode_database() -> None:
    version = ChunkingParameters(100, 20, 7).version
    assert "max=100" in version
    assert "min=20" in version
    assert f"unicode={unicodedata.unidata_version}" in version
    assert ChunkingParameters(100, 20, 7).version == ChunkingParameters(100, 20, 999).version
    assert ChunkingParameters(100, 21, 7).version != version


# ---- degenerate input ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocks", [[], [para("")], [para("   \n\t ")], [head("  "), para("\u00a0\u2003")]]
)
def test_nothing_to_chunk_is_a_permanent_rejection(blocks: list[SourceBlock]) -> None:
    with pytest.raises(ChunkingRejected) as excinfo:
        chunk_blocks(blocks, P)
    assert excinfo.value.code == NO_CHUNKABLE_TEXT


def test_whitespace_only_blocks_are_skipped_and_offsets_stay_in_the_original_block() -> None:
    blocks = [para("   "), para("  padded text  "), para("\n")]
    (chunk,) = chunk_blocks(blocks, P)
    assert chunk.text == "padded text"
    assert [(s.block_index, s.start, s.end) for s in chunk.segments] == [(1, 2, 13)]


def test_the_chunk_limit_is_enforced_while_chunking() -> None:
    blocks = [para("x" * 90) for _ in range(10)]
    with pytest.raises(ChunkingRejected) as excinfo:
        chunk_blocks(blocks, ChunkingParameters(100, 20, 9))
    assert excinfo.value.code == LIMIT_CHUNKS
    assert len(chunk_blocks(blocks, ChunkingParameters(100, 20, 10))) == 10


# ---- packing and boundaries ---------------------------------------------------------------------


def test_small_blocks_are_packed_in_order_up_to_the_ceiling() -> None:
    blocks = [para("a" * 40), para("b" * 40), para("c" * 40)]
    first, second = chunk_blocks(blocks, P)
    assert first.text == f"{'a' * 40}{SEPARATOR}{'b' * 40}"  # 82 characters
    assert second.text == "c" * 40
    assert [s.block_index for s in first.segments] == [0, 1]


def test_a_block_is_never_split_when_it_fits() -> None:
    blocks = [para("a" * 60), para("b" * 60)]
    chunks = chunk_blocks(blocks, P)
    assert [len(c.text) for c in chunks] == [60, 60]
    assert all(len(c.segments) == 1 for c in chunks)


def test_a_heading_opens_a_new_chunk_once_the_current_one_is_big_enough() -> None:
    blocks = [head("One"), para("a" * 30), head("Two"), para("b" * 30)]
    first, second = chunk_blocks(blocks, P)
    assert first.text.startswith("One") and "Two" not in first.text
    assert second.text.startswith("Two")
    assert second.section_path == ("Two",)


def test_a_heading_joins_a_small_chunk_instead_of_leaving_a_tiny_one() -> None:
    blocks = [para("short"), head("Two"), para("b" * 30)]
    (only,) = chunk_blocks(blocks, P)
    assert only.text.startswith("short")


def test_a_heading_is_never_left_alone_at_the_end_of_a_chunk() -> None:
    blocks = [para("a" * 80), head("Heading"), para("b" * 80)]
    first, second = chunk_blocks(blocks, P)
    assert first.text == "a" * 80
    assert second.text.startswith("Heading")


def test_consecutive_headings_travel_together_with_their_content() -> None:
    blocks = [para("a" * 70), head("Part", 1), head("Section", 2), para("b" * 30)]
    first, second = chunk_blocks(blocks, P)
    assert first.text == "a" * 70
    assert second.text.startswith(f"Part{SEPARATOR}Section")


def test_a_heading_stays_in_order_when_it_cannot_travel_with_its_content() -> None:
    blocks = [para("a" * 50), head("H" * 30), para("b" * 100)]
    texts = [c.text for c in chunk_blocks(blocks, P)]
    assert "".join(texts).replace(SEPARATOR, "") == "a" * 50 + "H" * 30 + "b" * 100


def test_section_path_follows_the_heading_hierarchy() -> None:
    blocks = [
        head("Article 1", 1),
        head("Section 1.1", 2),
        para("a" * 90),
        head("Section 1.2", 2),
        para("b" * 90),
        head("Article 2", 1),
        para("c" * 90),
    ]
    paths = [c.section_path for c in chunk_blocks(blocks, P)]
    assert paths[0] == ("Article 1",)  # the trail where the chunk begins, heading included
    assert ("Article 1", "Section 1.1") in paths
    assert ("Article 1", "Section 1.2") in paths
    assert paths[-1] == ("Article 2",)


def test_a_long_heading_label_is_cut_to_a_bounded_length() -> None:
    (chunk,) = chunk_blocks([head("T" * 5000), para("x")], ChunkingParameters(10_000, 20, 5))
    assert len(chunk.section_path[0]) == SECTION_LABEL_LIMIT


def test_pages_are_carried_as_a_range() -> None:
    blocks = [para("a" * 40, page=2), para("b" * 40, page=3), para("c" * 90)]
    first, second = chunk_blocks(blocks, P)
    assert (first.page_start, first.page_end) == (2, 3)
    assert (second.page_start, second.page_end) == (None, None)


# ---- oversized text -----------------------------------------------------------------------------


def test_an_oversized_block_is_cut_at_sentence_ends() -> None:
    sentence = "This is a clause that ends here. "
    text = (sentence * 12).strip()
    blocks = [para(text)]
    chunks = chunk_blocks(blocks, P)
    assert len(chunks) > 1
    assert all(c.text.endswith(".") for c in chunks)
    assert_sound(blocks)


def test_without_sentence_ends_the_cut_is_at_whitespace() -> None:
    text = " ".join(["word"] * 120)
    chunks = chunk_blocks([para(text)], P)
    assert all(not c.text.startswith(" ") and not c.text.endswith(" ") for c in chunks)
    assert all(c.text.split() == ["word"] * len(c.text.split()) for c in chunks)
    assert_sound([para(text)])


def test_an_unbroken_run_is_cut_at_the_ceiling() -> None:
    chunks = chunk_blocks([para("x" * 250)], P)
    assert [len(c.text) for c in chunks] == [100, 100, 50]


def test_cjk_full_stops_need_no_following_space() -> None:
    text = "这是一个句子。" * 30
    chunks = chunk_blocks([para(text)], P)
    assert all(c.text.endswith("。") for c in chunks)
    assert_sound([para(text)])


def test_a_hard_cut_never_separates_a_combining_mark() -> None:
    # Gurmukhi: base letter + vowel sign (Mc) + nasal sign (Mn); and Latin e + combining acute.
    text = "ਕਿੰ" * 80 + "e\u0301" * 80
    blocks = [para(unicodedata.normalize("NFC", text))]
    for chunk in chunk_blocks(blocks, P):
        assert not unicodedata.category(chunk.text[0]).startswith("M")
    assert_sound(blocks)


def test_a_hard_cut_never_separates_a_conjunct_or_a_joiner() -> None:
    virama = "\u094d"  # Devanagari virama: joins the next consonant
    # "xy" aligns the run so that a naive cut at 100 would fall right after a virama / ZWJ
    conjuncts = "xy" + ("क" + virama + "ष") * 70
    assert conjuncts[99] == virama
    for chunk in chunk_blocks([para(conjuncts)], P):
        assert not chunk.text.endswith(virama) and not chunk.text.startswith(virama)
    joined = "xy" + "a\u200db" * 70
    assert joined[99] == "\u200d"
    for chunk in chunk_blocks([para(joined)], P):
        assert not chunk.text.endswith("\u200d") and not chunk.text.startswith("\u200d")


def test_emoji_and_astral_characters_count_as_one_character_each() -> None:
    text = "\U0001f600" * 250
    chunks = chunk_blocks([para(text)], P)
    assert [len(c.text) for c in chunks] == [100, 100, 50]


def test_a_text_of_only_combining_marks_still_makes_progress() -> None:
    text = "\u0301" * 250
    assert_sound([para(text)])
    assert [len(c.text) for c in chunk_blocks([para(text)], P)] == [100, 100, 50]


def test_a_huge_unbroken_block_chunks_without_blowup() -> None:
    chunks = chunk_blocks([para("a" * 1_000_000)], ChunkingParameters(2000, 200, 10_000))
    assert len(chunks) == 500


# ---- determinism and invariants -----------------------------------------------------------------


def _random_blocks(rng: random.Random) -> list[SourceBlock]:
    alphabet = "abc def. ghi? ਕਿ é。 \u0301"
    blocks: list[SourceBlock] = []
    for _ in range(rng.randint(1, 12)):
        text = "".join(rng.choice(alphabet) for _ in range(rng.choice([3, 30, 90, 260]))).strip()
        level = rng.choice([None, None, 1, 2])
        blocks.append(SourceBlock(text or "x", level, rng.choice([None, 1, 2])))
    return blocks


@pytest.mark.parametrize("seed", range(40))
def test_random_documents_keep_every_invariant(seed: int) -> None:
    blocks = _random_blocks(random.Random(seed))  # noqa: S311  (seeded fixtures, not security)
    assert_sound(blocks)
    again = chunk_blocks(blocks, P)
    assert again == chunk_blocks(list(blocks), P)  # deterministic


def test_the_same_input_always_gives_the_same_chunks_and_text_hashes() -> None:
    blocks = [head("A"), para("lorem ipsum. " * 40), head("B"), para("dolor sit. " * 40)]
    first, second = chunk_blocks(blocks, P), chunk_blocks(blocks, P)
    assert first == second
    assert [c.text_hash for c in first] == [c.text_hash for c in second]


def test_a_lone_surrogate_does_not_crash_hashing() -> None:
    (chunk,) = chunk_blocks([para("a\ud800b")], P)
    assert len(chunk.text_hash) == 64


def test_chunk_text_is_not_in_a_repr() -> None:
    (chunk,) = chunk_blocks([para("Confidential indemnity")], P)
    assert "Confidential" not in repr(chunk)


# ---- exact boundaries ---------------------------------------------------------------------------


def test_blocks_that_fill_a_chunk_exactly_are_packed_together() -> None:
    blocks = [para("a" * 49), para("b" * 49)]  # 49 + 2 + 49 == 100 == the ceiling
    (only,) = chunk_blocks(blocks, P)
    assert len(only.text) == 100
    assert len(chunk_blocks([para("a" * 49), para("b" * 50)], P)) == 2  # one more: it splits


def test_a_heading_opens_a_section_exactly_at_the_minimum() -> None:
    at_minimum = [para("a" * 20), head("Two"), para("b" * 10)]
    assert len(chunk_blocks(at_minimum, P)) == 2  # 20 == min: the heading starts a chunk
    below = [para("a" * 19), head("Two"), para("b" * 10)]
    assert len(chunk_blocks(below, P)) == 1


def test_a_cut_prefers_whitespace_near_the_limit_over_an_early_sentence_end() -> None:
    text = "Short one. " + "word " * 40  # the only sentence end is far before the middle
    first, *_ = chunk_blocks([para(text)], P)
    assert len(first.text) > 90  # cut at whitespace close to the ceiling, not after "Short one."


def test_a_full_stop_inside_a_token_is_not_a_sentence_end() -> None:
    text = "a" * 79 + "." + "b" * 200  # e.g. a reference like "clause.7": no space follows
    first, *_ = chunk_blocks([para(text)], P)
    assert len(first.text) == 100
