from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from lda.modules.document_processing.config import DocumentProcessingSettings
from lda.modules.document_processing.domain.extraction import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionLimits,
    content_hash,
)
from lda.modules.document_processing.domain.failures import (
    PERMANENT_CODES,
    ExtractionRejected,
)
from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.modules.document_processing.domain.record import (
    ExtractionRecord,
    PurgeReceipt,
    Reconciliation,
    reconcile,
)
from lda.modules.document_processing.domain.resource import extracted_path
from lda.modules.document_processing.domain.text import normalize_text, paragraphs, single_line
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import TenantId

NOW = datetime(2026, 1, 1, tzinfo=UTC)
T, D, V = TenantId(UUID(int=1)), DocumentId(UUID(int=2)), VersionId(UUID(int=3))


def doc(*blocks: Block, pages: int | None = None) -> ExtractedDocument:
    return ExtractedDocument("text/plain", blocks, pages)


def para(text: str = "Hello", **kw: int | None) -> Block:
    return Block(BlockKind.PARAGRAPH, text, **kw)


# ---- normalization -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\r\nb\rc", "a\nb\nc"),
        ("  a \t  b  ", "a b"),
        ("a\u00a0\u2003b", "a b"),  # no-break and em space
        ("a\u200bb\ufeffc\u202ed", "abcd"),  # zero-width, BOM, bidi override
        ("a\x00b\x07c", "abc"),  # controls
        ("a\ud800b", "ab"),  # lone surrogate
        ("e\u0301", "\u00e9"),  # NFC
        ("a\n\n\n\nb", "a\n\nb"),
        ("\n\n a \n\n", "a"),
        ("a\u2028b", "a b"),
        ("", ""),
        ("\u200b \t", ""),
    ],
)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_normalization_is_idempotent() -> None:
    raw = "  Fée\u00a0\u200b\r\n\r\n\r\n x\ty  "
    once = normalize_text(raw)
    assert normalize_text(once) == once


def test_paragraphs_split_on_blank_lines_and_join_layout_lines() -> None:
    assert paragraphs("one\ntwo\n\nthree\n\n\nfour") == ("one two", "three", "four")
    assert paragraphs(" \n\u200b ") == ()


def test_single_line_joins_every_break() -> None:
    assert single_line("a\n\nb\n c ") == "a b c"


# ---- blocks and documents ----------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", " x", "x ", "a\nb"])
def test_block_text_must_be_clean_single_line(text: str) -> None:
    with pytest.raises(ValidationError):
        Block(BlockKind.PARAGRAPH, text)


@pytest.mark.parametrize(
    ("kind", "level"),
    [
        (BlockKind.HEADING, None),
        (BlockKind.PARAGRAPH, 1),
        (BlockKind.LIST_ITEM, 2),
        (BlockKind.HEADING, 0),
        (BlockKind.HEADING, 10),
    ],
)
def test_block_level_only_for_headings_within_range(kind: BlockKind, level: int | None) -> None:
    with pytest.raises(ValidationError):
        Block(kind, "x", level=level)


def test_heading_levels_one_to_nine_are_valid() -> None:
    assert Block(BlockKind.HEADING, "x", level=1).level == 1
    assert Block(BlockKind.HEADING, "x", level=9).level == 9


def test_block_page_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Block(BlockKind.PARAGRAPH, "x", page=0)


def test_document_needs_blocks_and_consistent_pages() -> None:
    with pytest.raises(ValidationError):
        ExtractedDocument("text/plain", ())
    with pytest.raises(ValidationError):
        doc(para(page=1))  # a page without a page count
    with pytest.raises(ValidationError):
        doc(para(page=3), pages=2)
    with pytest.raises(ValidationError):
        doc(para(), pages=0)
    assert doc(para(page=2), pages=2).page_count == 2


def test_character_count_sums_block_text() -> None:
    assert doc(para("abc"), para("de")).character_count == 5


def test_artifact_round_trips_and_is_canonical() -> None:
    original = doc(
        Block(BlockKind.HEADING, "Titre é", level=2, page=1),
        para("Body", page=1),
        Block(BlockKind.LIST_ITEM, "item", page=2),
        pages=2,
    )
    data = original.to_bytes()
    assert data == (
        b'{"blocks":[{"kind":"heading","level":2,"page":1,"text":"Titre \\u00e9"},'
        b'{"kind":"paragraph","level":null,"page":1,"text":"Body"},'
        b'{"kind":"list_item","level":null,"page":2,"text":"item"}],'
        b'"media_type":"text/plain","page_count":2,"schema":1}'
    )
    assert ExtractedDocument.from_bytes(data) == original
    assert data == ExtractedDocument.from_bytes(data).to_bytes()


def test_hash_is_sha256_of_the_artifact_and_changes_with_content() -> None:
    data = doc(para("a")).to_bytes()
    assert len(content_hash(data)) == 64
    assert content_hash(data) != content_hash(doc(para("b")).to_bytes())
    assert content_hash(data) == content_hash(doc(para("a")).to_bytes())


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json",
        b"[]",
        b'{"schema":1}',
        b'{"schema":2,"media_type":"x","page_count":null,"blocks":[]}',
    ],
)
def test_corrupt_artifact_is_rejected(data: bytes) -> None:
    with pytest.raises(ValidationError):
        ExtractedDocument.from_bytes(data)


def test_limits_must_be_positive() -> None:
    for args in [(1, 1, 1, 0), (0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 0, 1)]:
        with pytest.raises(ValidationError) as excinfo:
            ExtractionLimits(*args)
        assert excinfo.value.code == "extraction_limits_invalid"


# ---- records, reconciliation, receipts ---------------------------------------------------------


def record(extracted: ExtractedDocument, parser: str = "extract-1") -> ExtractionRecord:
    return ExtractionRecord.describe(
        tenant_id=T,
        document_id=D,
        version_id=V,
        parser_version=parser,
        extracted=extracted,
        now=NOW,
    )[0]


def test_describe_records_counts_and_hash_never_text() -> None:
    extracted = doc(para("secret clause", page=1), pages=1)
    rec, artifact = ExtractionRecord.describe(
        tenant_id=T,
        document_id=D,
        version_id=V,
        parser_version="extract-1",
        extracted=extracted,
        now=NOW,
    )
    assert rec.content_hash == content_hash(artifact)
    assert (rec.artifact_bytes, rec.block_count, rec.character_count, rec.page_count) == (
        len(artifact),
        1,
        13,
        1,
    )
    assert "secret" not in repr(rec)


@pytest.mark.parametrize(
    "change",
    [
        {"content_hash": "A" * 64},
        {"content_hash": "a" * 63},
        {"parser_version": ""},
        {"parser_version": "x\n"},
        {"artifact_bytes": 0},
        {"block_count": 0},
        {"character_count": 0},
        {"page_count": 0},
        {"created_at": datetime(2026, 1, 1)},
    ],
)
def test_record_invariants(change: dict[str, object]) -> None:
    import dataclasses

    with pytest.raises(ValidationError):
        dataclasses.replace(record(doc(para())), **change)  # type: ignore[arg-type]


def test_reconcile_matrix() -> None:
    base = record(doc(para("a")))
    assert reconcile(base, record(doc(para("a")))) is Reconciliation.ADOPT
    assert reconcile(base, record(doc(para("b")))) is Reconciliation.CONFLICT
    assert (
        reconcile(base, record(doc(para("b")), parser="extract-2")) is Reconciliation.KEEP_RECORDED
    )
    assert (
        reconcile(base, record(doc(para("a")), parser="extract-2")) is Reconciliation.KEEP_RECORDED
    )


def test_receipt_counts_versions() -> None:
    receipt = PurgeReceipt(T, D, NOW, (V, VersionId(UUID(int=4))))
    assert receipt.versions_purged == 2
    assert receipt.scope == "document_processing.extracted_artifacts_and_rows"


def test_artifact_path_is_identifiers_only() -> None:
    assert extracted_path(D, V) == f"document_processing/{D}/{V}/extracted"


# ---- failures and config -----------------------------------------------------------------------


def test_only_known_codes_can_be_raised() -> None:
    with pytest.raises(ValueError):
        ExtractionRejected("made_up")
    for code in PERMANENT_CODES:
        assert ExtractionRejected(code).code == code


def test_settings_defaults_and_validation() -> None:
    settings = DocumentProcessingSettings()
    assert (settings.max_bytes, settings.max_pages) == (50 * 1024 * 1024, 1000)
    with pytest.raises(ValueError):
        DocumentProcessingSettings(max_pages=0)
    with pytest.raises(ValueError):
        DocumentProcessingSettings(unknown=1)  # type: ignore[call-arg]


def test_ascii_punctuation_survives_next_to_non_ascii_text() -> None:
    """A line with a non-ASCII character takes the per-character path: only whitespace and
    dropped categories change there, every ASCII punctuation mark stays."""
    import string

    line = "caf\u00e9 " + string.punctuation
    assert normalize_text(line) == line
    assert normalize_text("a~\u00e9\t~") == "a~\u00e9 ~"
