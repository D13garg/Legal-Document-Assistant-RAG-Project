"""Hand-built documents for the tests: a minimal PDF writer and a DOCX writer (no extra library)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)


def _escape_pdf(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: Sequence[Sequence[str] | None], *, extra_trailer: str = "") -> bytes:
    """One page per entry; each entry is the lines of that page (None or empty: a page without
    any text, like a scan)."""
    objects: list[bytes] = []
    page_ids = [4 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for index, lines in enumerate(pages):
        content_id = page_ids[index] + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
        )
        if lines:
            body = "BT /F1 12 Tf 72 720 Td 14 TL " + " T* ".join(
                f"({_escape_pdf(line)}) Tj" for line in lines
            )
            content = (body + " ET").encode("latin-1")
        else:
            content = b""
        objects.append(
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R {extra_trailer} >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


@dataclass(frozen=True)
class Para:
    text: str
    style: str | None = None
    numbered: bool = False


def _para_xml(para: Para) -> str:
    props = ""
    if para.style or para.numbered:
        style = f'<w:pStyle w:val="{para.style}"/>' if para.style else ""
        numbering = (
            '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>' if para.numbered else ""
        )
        props = f"<w:pPr>{style}{numbering}</w:pPr>"
    text = para.text.replace("&", "&amp;").replace("<", "&lt;")
    return f'<w:p>{props}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def document_xml(body: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}">'
        f"<w:body>{body}</w:body></w:document>"
    )


def make_docx(
    paragraphs: Sequence[Para],
    *,
    xml: str | None = None,
    extra_parts: dict[str, bytes] | None = None,
    omit: tuple[str, ...] = (),
) -> bytes:
    main = xml if xml is not None else document_xml("".join(_para_xml(p) for p in paragraphs))
    parts: dict[str, bytes] = {
        "[Content_Types].xml": CONTENT_TYPES.encode(),
        "word/document.xml": main.encode("utf-8"),
    }
    parts.update(extra_parts or {})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            if name not in omit:
                archive.writestr(name, data)
    return out.getvalue()
