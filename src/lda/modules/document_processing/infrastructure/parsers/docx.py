"""DOCX: the main document part only, read with hard caps and a hardened XML parser (ADR-037).

Out of scope on purpose: headers, footers, footnotes, comments, tracked deletions and embedded
objects. A table is read as its cell paragraphs in document order.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from lda.modules.document_processing.domain.extraction import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionLimits,
)
from lda.modules.document_processing.domain.failures import (
    EMPTY_DOCUMENT,
    ENCRYPTED_DOCUMENT,
    LIMIT_CHARACTERS,
    LIMIT_EXPANDED_BYTES,
    MALFORMED_DOCUMENT,
    ExtractionRejected,
)
from lda.modules.document_processing.domain.text import single_line

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAIN_PART = "word/document.xml"
CONTENT_TYPES_PART = "[Content_Types].xml"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_HEADING_STYLE = re.compile(r"heading\s*([1-9])", re.IGNORECASE)
_TEXT_TAGS = {f"{_W}tab": "\t", f"{_W}br": "\n", f"{_W}cr": "\n", f"{_W}noBreakHyphen": "-"}


def parse_docx(content: bytes, limits: ExtractionLimits) -> ExtractedDocument:
    root = _read_main_part(content, limits)
    blocks: list[Block] = []
    total = 0
    for paragraph in _paragraphs(root):
        text = single_line(_own_text(paragraph))
        if not text:
            continue
        total += len(text)
        if total > limits.max_characters:
            raise ExtractionRejected(LIMIT_CHARACTERS)
        level = _heading_level(paragraph)
        if level is not None:
            blocks.append(Block(BlockKind.HEADING, text, level=level))
        elif paragraph.find(f"{_W}pPr/{_W}numPr") is not None:
            blocks.append(Block(BlockKind.LIST_ITEM, text))
        else:
            blocks.append(Block(BlockKind.PARAGRAPH, text))
    if not blocks:
        raise ExtractionRejected(EMPTY_DOCUMENT)
    return ExtractedDocument(DOCX, tuple(blocks))


def _read_main_part(content: bytes, limits: ExtractionLimits) -> Element:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(set(names)) != len(names):
                raise ExtractionRejected(MALFORMED_DOCUMENT)  # ambiguous entries
            if CONTENT_TYPES_PART not in names or MAIN_PART not in names:
                raise ExtractionRejected(MALFORMED_DOCUMENT)
            if sum(info.file_size for info in infos) > limits.max_expanded_bytes:
                raise ExtractionRejected(LIMIT_EXPANDED_BYTES)  # declared sizes: cheap first check
            with archive.open(MAIN_PART) as part:
                data = (
                    part.read()
                )  # zipfile never returns more than the declared size, checked above
    except ExtractionRejected:
        raise
    except RuntimeError:
        raise ExtractionRejected(ENCRYPTED_DOCUMENT) from None  # zipfile: "password required"
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, OSError, ValueError):
        raise ExtractionRejected(MALFORMED_DOCUMENT) from None
    try:
        root: Element = fromstring(data, forbid_dtd=True)
        return root
    except (ParseError, DefusedXmlException, ValueError):
        raise ExtractionRejected(MALFORMED_DOCUMENT) from None


def _paragraphs(root: Element) -> list[Element]:
    """Every `w:p` in document order, including paragraphs nested in text boxes. Iterative, so a
    deeply nested document cannot exhaust the interpreter stack."""
    found: list[Element] = []
    stack = [root]
    while stack:
        element = stack.pop()
        if element.tag == _MC_FALLBACK:
            continue  # the duplicate of an alternate content branch
        if element.tag == f"{_W}p":
            found.append(element)
        stack.extend(reversed(list(element)))
    return found


def _own_text(paragraph: Element) -> str:
    """The paragraph's own runs (not those of a nested paragraph)."""
    parts: list[str] = []
    stack = list(reversed(list(paragraph)))
    while stack:
        element = stack.pop()
        if element.tag in (f"{_W}p", _MC_FALLBACK):
            continue
        if element.tag == f"{_W}t":
            parts.append(element.text or "")
        elif element.tag in _TEXT_TAGS:
            parts.append(_TEXT_TAGS[element.tag])
        stack.extend(reversed(list(element)))
    return "".join(parts)


def _heading_level(paragraph: Element) -> int | None:
    style = paragraph.find(f"{_W}pPr/{_W}pStyle")
    value = style.get(f"{_W}val") if style is not None else None
    if value is None:
        return None
    if value.lower() == "title":
        return 1
    match = _HEADING_STYLE.fullmatch(value)
    return int(match.group(1)) if match else None
