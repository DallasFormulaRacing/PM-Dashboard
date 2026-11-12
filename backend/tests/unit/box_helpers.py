"""Synthetic Box test helpers: tiny PDFs/DOCX built in memory, and a fake auth."""

from __future__ import annotations

from pathlib import Path


def make_pdf(pages: list[list[str]]) -> bytes:
    """Build a minimal valid PDF; each page is a list of text lines (Helvetica)."""

    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects: list[bytes] = []
    page_count = len(pages)
    font_id = 3
    first_page_id = 4
    kids = " ".join(f"{first_page_id + 2 * i} 0 R" for i in range(page_count))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for index, lines in enumerate(pages):
        content_id = first_page_id + 2 * index + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
        )
        if lines:
            ops = ["BT", "/F1 11 Tf", "14 TL", "72 740 Td"]
            for line in lines:
                ops.append(f"({escape(line)}) Tj T*")
            ops.append("ET")
            stream = "\n".join(ops).encode("latin-1")
        else:
            stream = b""
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_docx(path: Path) -> None:
    """Synthetic DOCX: headings, prose, a bullet list, and a table."""

    import docx

    document = docx.Document()
    document.add_heading("Synthetic Cooling Guide", level=1)
    document.add_paragraph("This fixture describes a fictional coolant loop.")
    document.add_heading("Pump Selection", level=2)
    document.add_paragraph("Choose the fictional pump with the lowest draw.")
    document.add_paragraph("Check flow rate", style="List Bullet")
    document.add_paragraph("Check | pipe characters", style="List Bullet")
    _add_hyperlink(
        document.add_paragraph("Reference: "), "Synthetic pump datasheet", "https://example.com/pump"
    )
    document.add_heading("Limits", level=2)
    table = document.add_table(rows=3, cols=2)
    for row, values in enumerate([("Parameter", "Limit"), ("Temp", "60 C"), ("Flow", "8 L/min")]):
        for col, value in enumerate(values):
            table.cell(row, col).text = value
    document.save(str(path))


def _add_hyperlink(paragraph, text: str, url: str) -> None:
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    rel_id = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    node = OxmlElement("w:t")
    node.text = text
    run.append(node)
    link.append(run)
    paragraph._p.append(link)


class FakeAuth:
    """Returns a fixed token; counts invalidations."""

    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens or ["synthetic-access-token"]
        self.invalidations = 0

    async def get_access_token(self) -> str:
        return self.tokens[min(self.invalidations, len(self.tokens) - 1)]

    async def invalidate(self) -> None:
        self.invalidations += 1
