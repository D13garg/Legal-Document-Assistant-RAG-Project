from __future__ import annotations

import io
import re
import zipfile

import pytest

from lda.modules.document_processing.domain.extraction import BlockKind, ExtractionLimits
from lda.modules.document_processing.domain.failures import ExtractionRejected
from lda.modules.document_processing.infrastructure.parsers.docx import DOCX
from lda.modules.document_processing.infrastructure.parsers.pdf import PDF
from lda.modules.document_processing.infrastructure.parsers.registry import (
    PARSER_VERSION,
    FormatParsers,
)
from lda.modules.document_processing.tests.builders import (
    W_NS,
    Para,
    document_xml,
    make_docx,
    make_pdf,
)

LIMITS = ExtractionLimits(
    max_bytes=1_000_000, max_pages=5, max_characters=1000, max_expanded_bytes=100_000
)
PARSERS = FormatParsers()
TEXT = "text/plain"


def code_of(media_type: str, content: bytes, limits: ExtractionLimits = LIMITS) -> str:
    with pytest.raises(ExtractionRejected) as excinfo:
        PARSERS.parse(media_type, content, limits)
    return excinfo.value.code


# ---- registry ----------------------------------------------------------------------------------


def test_version_names_schema_and_libraries() -> None:
    assert PARSERS.version == PARSER_VERSION
    assert PARSER_VERSION.startswith("extract-1; pypdf=") and "defusedxml=" in PARSER_VERSION


def test_unsupported_media_type_is_refused_before_anything_else() -> None:
    assert code_of("image/png", b"") == "unsupported_media_type"
    assert code_of("text/html", b"<p>x</p>") == "unsupported_media_type"
    assert code_of("application/pdf; charset=x", b"x") == "unsupported_media_type"


def test_empty_and_oversized_content() -> None:
    assert code_of(TEXT, b"") == "empty_document"
    assert code_of(TEXT, b"x" * 11, ExtractionLimits(10, 5, 1000, 1000)) == "limit_exceeded.bytes"
    assert PARSERS.parse(TEXT, b"x" * 10, ExtractionLimits(10, 5, 1000, 1000)).blocks


def test_type_is_decided_by_media_type_not_by_content() -> None:
    assert code_of(PDF, b"just text") == "malformed_document"
    assert code_of(DOCX, make_pdf([["x"]])) == "malformed_document"


# ---- text/plain --------------------------------------------------------------------------------


def test_plain_text_paragraphs() -> None:
    result = PARSERS.parse(TEXT, b"Clause one\ncontinues.\n\n\nClause two.\n", LIMITS)
    assert [b.text for b in result.blocks] == ["Clause one continues.", "Clause two."]
    assert all(b.kind is BlockKind.PARAGRAPH and b.page is None for b in result.blocks)
    assert result.page_count is None and result.media_type == TEXT


def test_plain_text_encodings() -> None:
    assert PARSERS.parse(TEXT, "\ufeffcafé".encode(), LIMITS).blocks[0].text == "café"
    assert PARSERS.parse(TEXT, "hé".encode("utf-16"), LIMITS).blocks[0].text == "hé"
    assert PARSERS.parse(TEXT, b"\xfe\xff\x00h\x00\xe9", LIMITS).blocks[0].text == "hé"


def test_plain_text_invalid_utf8_and_binary_are_malformed() -> None:
    assert code_of(TEXT, b"caf\xe9") == "malformed_document"  # latin-1 is not guessed
    assert code_of(TEXT, b"ab\x00cd") == "malformed_document"


def test_plain_text_without_text_is_empty() -> None:
    assert code_of(TEXT, b" \n\t\n ") == "empty_document"
    assert code_of(TEXT, "\u200b\ufeff".encode()) == "empty_document"


def test_plain_text_character_limit_counts_normalized_text() -> None:
    limits = ExtractionLimits(10_000, 5, 10, 1000)
    assert PARSERS.parse(TEXT, b"a" * 10, limits).character_count == 10
    assert code_of(TEXT, b"a" * 11, limits) == "limit_exceeded.characters"
    assert code_of(TEXT, b"aaaaaa\n\nbbbbb", limits) == "limit_exceeded.characters"  # across blocks
    assert PARSERS.parse(TEXT, b"a    " + b"b" * 5 + b"\n\n\n\n", limits).character_count == 7


# ---- DOCX --------------------------------------------------------------------------------------


def test_docx_structure_headings_lists_paragraphs() -> None:
    data = make_docx(
        [
            Para("Agreement", "Title"),
            Para("1. Definitions", "Heading1"),
            Para("Sub", "heading 3"),
            Para("The parties agree."),
            Para("first item", numbered=True),
            Para("Numbered heading", "Heading2", numbered=True),
            Para("   "),
            Para("Unknown style", "Quote"),
        ]
    )
    result = PARSERS.parse(DOCX, data, LIMITS)
    assert [(b.kind, b.text, b.level) for b in result.blocks] == [
        (BlockKind.HEADING, "Agreement", 1),
        (BlockKind.HEADING, "1. Definitions", 1),
        (BlockKind.HEADING, "Sub", 3),
        (BlockKind.PARAGRAPH, "The parties agree.", None),
        (BlockKind.LIST_ITEM, "first item", None),
        (BlockKind.HEADING, "Numbered heading", 2),
        (BlockKind.PARAGRAPH, "Unknown style", None),
    ]
    assert result.page_count is None


@pytest.mark.parametrize("style", ["Heading0", "Heading10", "Heading1x", "NotAHeading1"])
def test_docx_non_heading_styles_are_paragraphs(style: str) -> None:
    result = PARSERS.parse(DOCX, make_docx([Para("x", style)]), LIMITS)
    assert result.blocks[0].kind is BlockKind.PARAGRAPH


def test_docx_runs_tabs_breaks_and_nested_text_boxes() -> None:
    body = (
        "<w:p><w:r><w:t>Hel</w:t></w:r><w:r><w:t>lo</w:t></w:r><w:r><w:tab/></w:r>"
        "<w:r><w:t>world</w:t><w:br/><w:t>again</w:t><w:noBreakHyphen/><w:t>x</w:t></w:r>"
        "<w:del><w:r><w:delText>deleted</w:delText></w:r></w:del></w:p>"
        "<w:p><w:r><w:t>outer</w:t></w:r><w:r><w:pict><w:txbxContent>"
        "<w:p><w:r><w:t>inside box</w:t></w:r></w:p></w:txbxContent></w:pict></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell A</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>cell B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    result = PARSERS.parse(DOCX, make_docx([], xml=document_xml(body)), LIMITS)
    assert [b.text for b in result.blocks] == [
        "Hello world again-x",
        "outer",
        "inside box",
        "cell A",
        "cell B",
    ]


def test_docx_alternate_content_fallback_is_not_duplicated() -> None:
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    xml = (
        f'<?xml version="1.0"?><w:document xmlns:w="{W_NS}" xmlns:mc="{mc}"><w:body>'
        "<mc:AlternateContent><mc:Choice><w:p><w:r><w:t>choice</w:t></w:r></w:p></mc:Choice>"
        "<mc:Fallback><w:p><w:r><w:t>fallback</w:t></w:r></w:p></mc:Fallback>"
        "</mc:AlternateContent></w:body></w:document>"
    )
    result = PARSERS.parse(DOCX, make_docx([], xml=xml), LIMITS)
    assert [b.text for b in result.blocks] == ["choice"]


def test_docx_deep_nesting_does_not_exhaust_the_stack() -> None:
    depth = 3000
    xml = document_xml(
        "<w:tbl><w:tr><w:tc>" * depth
        + "<w:p><w:r><w:t>deep</w:t></w:r></w:p>"
        + "</w:tc></w:tr></w:tbl>" * depth
    )
    limits = ExtractionLimits(10_000_000, 5, 1000, 10_000_000)
    try:
        result = PARSERS.parse(DOCX, make_docx([], xml=xml), limits)
    except ExtractionRejected as exc:  # the XML parser itself may refuse such depth
        assert exc.code == "malformed_document"
    else:
        assert [b.text for b in result.blocks] == ["deep"]


def test_docx_empty_and_whitespace_only_is_empty() -> None:
    assert code_of(DOCX, make_docx([])) == "empty_document"
    assert code_of(DOCX, make_docx([Para(" "), Para("\u200b")])) == "empty_document"


def test_docx_character_limit() -> None:
    limits = ExtractionLimits(1_000_000, 5, 10, 100_000)
    assert PARSERS.parse(DOCX, make_docx([Para("a" * 10)]), limits).character_count == 10
    assert (
        code_of(DOCX, make_docx([Para("a" * 6), Para("b" * 5)]), limits)
        == "limit_exceeded.characters"
    )


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04 not really a zip",
        make_docx([Para("x")])[:-30],  # truncated archive
        make_docx([Para("x")], omit=("[Content_Types].xml",)),
        make_docx([Para("x")], omit=("word/document.xml",)),
        make_docx([], xml="<not-closed"),
        make_docx([], xml='<?xml version="1.0"?><w:document xmlns:w="x"><w:body></w:document>'),
    ],
)
def test_docx_malformed_inputs(data: bytes) -> None:
    assert code_of(DOCX, data) == "malformed_document"


def test_docx_empty_bytes_are_empty_document() -> None:
    assert code_of(DOCX, b"") == "empty_document"


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_docx_duplicate_entries_are_malformed() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document_xml("<w:p><w:r><w:t>a</w:t></w:r></w:p>"))
        archive.writestr("word/document.xml", document_xml("<w:p><w:r><w:t>b</w:t></w:r></w:p>"))
    assert code_of(DOCX, out.getvalue()) == "malformed_document"


def test_docx_corrupt_compressed_data_is_malformed() -> None:
    good = bytearray(make_docx([Para("x" * 200)]))
    start = good.index(b"word/document.xml") + len(b"word/document.xml") + 4
    for i in range(start, start + 8):
        good[i] ^= 0xFF
    assert code_of(DOCX, bytes(good)) == "malformed_document"


def test_docx_expanded_size_is_capped_by_declared_size() -> None:
    limits = ExtractionLimits(10_000_000, 5, 10_000_000, 1000)
    big = make_docx([Para("a" * 5000)])  # the archive is far smaller than the 1000-byte cap
    assert len(big) < 1000
    assert code_of(DOCX, big, limits) == "limit_exceeded.expanded_bytes"


def test_docx_forged_declared_size_cannot_bypass_the_cap() -> None:
    limits = ExtractionLimits(10_000_000, 5, 10_000_000, 1000)
    forged = bytearray(make_docx([Para("a" * 5000)]))
    for marker, offset in ((b"PK\x01\x02", 24), (b"PK\x03\x04", 22)):
        index = 0
        while (index := bytes(forged).find(marker, index)) != -1:
            forged[index + offset : index + offset + 4] = (10).to_bytes(4, "little")
            index += 4
    # zipfile honours the (lying) declared size, so the data is truncated and unusable
    assert code_of(DOCX, bytes(forged), limits) == "malformed_document"


def test_docx_exact_expanded_limit_is_allowed() -> None:
    data = make_docx([Para("a")])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        total = sum(i.file_size for i in archive.infolist())
        part = archive.getinfo("word/document.xml").file_size
    assert PARSERS.parse(DOCX, data, ExtractionLimits(10**6, 5, 1000, total)).blocks
    assert (
        code_of(DOCX, data, ExtractionLimits(10**6, 5, 1000, total - 1))
        == "limit_exceeded.expanded_bytes"
    )
    assert part < total


def test_docx_dtd_and_entities_are_refused() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r>'
        "<w:t>&b;</w:t></w:r></w:p></w:body></w:document>"
    )
    external = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r>'
        "<w:t>&x;</w:t></w:r></w:p></w:body></w:document>"
    )
    assert code_of(DOCX, make_docx([], xml=bomb)) == "malformed_document"
    assert code_of(DOCX, make_docx([], xml=external)) == "malformed_document"


def test_docx_encrypted_zip_is_encrypted() -> None:
    data = bytearray(make_docx([Para("secret")]))
    for marker, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        index = 0
        while (index := bytes(data).find(marker, index)) != -1:
            data[index + offset] |= 0x01  # general purpose flag: encrypted
            index += 4
    assert code_of(DOCX, bytes(data)) == "encrypted_document"


# ---- PDF ---------------------------------------------------------------------------------------


def test_pdf_text_with_pages() -> None:
    data = make_pdf([["Clause one", "continues here"], ["Page two (with parens)"]])
    result = PARSERS.parse(PDF, data, LIMITS)
    assert result.page_count == 2
    assert [(b.text, b.page) for b in result.blocks] == [
        ("Clause one continues here", 1),
        ("Page two (with parens)", 2),
    ]
    assert all(b.kind is BlockKind.PARAGRAPH for b in result.blocks)


def test_pdf_pages_without_text_are_skipped_but_counted() -> None:
    result = PARSERS.parse(PDF, make_pdf([["a"], None, ["c"]]), LIMITS)
    assert result.page_count == 3
    assert [b.page for b in result.blocks] == [1, 3]


def test_pdf_without_any_text_layer_is_no_extractable_text() -> None:
    assert code_of(PDF, make_pdf([None, None])) == "no_extractable_text"


def test_pdf_page_limit_is_decided_before_extraction() -> None:
    data = make_pdf([["x"]] * 6)
    assert code_of(PDF, data) == "limit_exceeded.pages"
    assert PARSERS.parse(PDF, make_pdf([["x"]] * 5), LIMITS).page_count == 5


def test_pdf_character_limit() -> None:
    limits = ExtractionLimits(1_000_000, 5, 10, 1000)
    assert PARSERS.parse(PDF, make_pdf([["a" * 10]]), limits).character_count == 10
    assert code_of(PDF, make_pdf([["a" * 6], ["b" * 5]]), limits) == "limit_exceeded.characters"


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.4\ngarbage", b"%PDF-", b"\x00\x01\x02", make_pdf([["x"]])[:60], b"%PDF-1.4\n%%EOF"],
)
def test_pdf_malformed_inputs(data: bytes) -> None:
    assert code_of(PDF, data) in {"malformed_document", "empty_document"}


def test_pdf_encrypted_is_refused() -> None:
    trailer = (
        "/Encrypt << /Filter /Standard /V 1 /R 2 /O ("
        + "a" * 32
        + ") /U ("
        + "b" * 32
        + ") /P -4 >>"
    )
    data = make_pdf([["secret"]], extra_trailer=trailer + " /ID [<00><00>]")
    assert code_of(PDF, data) == "encrypted_document"


# ---- gaps found by the mutation campaign -------------------------------------------------------


def test_docx_doctype_without_entities_is_refused() -> None:
    """`forbid_dtd` is policy on its own: a bare DOCTYPE has no entity for `forbid_entities` to
    catch, yet a DTD is never legitimate in a DOCX main part."""
    plain = (
        '<?xml version="1.0"?><!DOCTYPE d>'
        f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r>'
        "<w:t>x</w:t></w:r></w:p></w:body></w:document>"
    )
    assert code_of(DOCX, make_docx([], xml=plain)) == "malformed_document"


def test_pdf_without_pages_is_an_empty_document() -> None:
    assert code_of(PDF, make_pdf([])) == "empty_document"


def test_pdf_with_a_recoverable_defect_is_still_extracted() -> None:
    """A wrong `startxref` pointer is repaired by pypdf in lenient mode (and only there)."""
    damaged = re.sub(rb"startxref\n\d+", b"startxref\n7", make_pdf([["Hello world"]]))
    extracted = PARSERS.parse(PDF, damaged, LIMITS)
    assert [b.text for b in extracted.blocks] == ["Hello world"]


def test_pdf_page_that_cannot_be_read_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    from pypdf import PageObject

    def broken(self: PageObject, *args: object, **kwargs: object) -> str:
        raise ValueError("unreadable content stream")

    monkeypatch.setattr(PageObject, "extract_text", broken)
    assert code_of(PDF, make_pdf([["Hello"]])) == "malformed_document"
