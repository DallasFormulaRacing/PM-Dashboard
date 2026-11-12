"""Lightweight, local text extraction for approved Box files.

PDF: pypdf text per page (no OCR). DOCX: python-docx headings, lists, and tables.
MD/TXT: UTF-8 Markdown heading sections. Docling is intentionally not used yet
(no PyTorch builds for this Intel macOS host); keep the `extract_file` contract so a
Docling implementation can be swapped in and compared on the evaluation set.

Output text is Markdown consumed by the shared chunker, so plain extracted text is
escaped where a line start would otherwise be parsed as Markdown structure.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re

from app.domain.documents import DocumentSection
from app.ingestion.notion_markdown import MarkdownExtractionError, clean_notion_markdown


SUPPORTED_EXTENSIONS = frozenset({"pdf", "docx", "md", "txt"})
# A page with fewer non-whitespace characters is treated as having no text layer.
MIN_PAGE_TEXT_CHARS = 20
EXTRACTOR_VERSION = "box-light-v1"


class BoxExtractionError(ValueError):
    """The file cannot be converted into trustworthy text sections."""


class BoxUnsupportedContentError(BoxExtractionError):
    """Content exists but needs an extraction capability we do not have (e.g. OCR)."""


@dataclass
class ExtractionResult:
    sections: list[DocumentSection]
    parser: str
    warnings: list[str] = field(default_factory=list)

    def metadata(self) -> dict[str, object]:
        return {
            "extractor": EXTRACTOR_VERSION,
            "parser": self.parser,
            "parser_version": _package_version(self.parser),
            "extraction_warnings": list(self.warnings),
        }


def extract_file(path: Path, extension: str) -> ExtractionResult:
    extension = extension.lower().lstrip(".")
    if extension == "pdf":
        return extract_pdf(path)
    if extension == "docx":
        return extract_docx(path)
    if extension in {"md", "txt"}:
        return extract_markdown(path, plain_text=extension == "txt")
    raise BoxUnsupportedContentError(f"No extractor for .{extension} files")


# ---------------------------------------------------------------- PDF


def extract_pdf(path: Path) -> ExtractionResult:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise BoxUnsupportedContentError("Encrypted PDFs are not supported")
        pages = [page.extract_text() or "" for page in reader.pages]
    except BoxExtractionError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError) as error:
        raise BoxExtractionError(f"PDF could not be parsed: {type(error).__name__}") from error

    if not pages:
        raise BoxExtractionError("PDF has no pages")
    sections: list[DocumentSection] = []
    empty_pages: list[int] = []
    for number, raw in enumerate(pages, start=1):
        text = _escape_plain_text(raw)
        if len(re.sub(r"\s", "", text)) < MIN_PAGE_TEXT_CHARS:
            empty_pages.append(number)
            continue
        sections.append(
            DocumentSection(
                text=text,
                page_number=number,
                section_reference=f"pdf:page:{number}",
            )
        )
    if not sections:
        raise BoxUnsupportedContentError(
            "PDF has no extractable text layer (scanned/image-only); OCR is not enabled"
        )
    warnings = []
    if empty_pages:
        warnings.append(
            f"{len(empty_pages)} of {len(pages)} PDF pages had no extractable text "
            f"(pages {_page_ranges(empty_pages)}); their content is not indexed"
        )
    return ExtractionResult(sections, "pypdf", warnings)


# ---------------------------------------------------------------- DOCX


def extract_docx(path: Path) -> ExtractionResult:
    import docx
    from docx.opc.exceptions import PackageNotFoundError as DocxPackageError

    try:
        document = docx.Document(str(path))
    except (DocxPackageError, ValueError, KeyError, OSError) as error:
        raise BoxExtractionError(f"DOCX could not be parsed: {type(error).__name__}") from error

    sections: list[DocumentSection] = []
    headings: dict[int, str] = {}
    body: list[str] = []
    images = 0
    block_index = 0
    first_block = 0

    def flush(end: int) -> None:
        if body:
            sections.append(
                DocumentSection(
                    text="\n\n".join(body),
                    heading_path=[headings[level] for level in sorted(headings)],
                    section_reference=f"docx:blocks:{first_block + 1}-{end}",
                )
            )
        body.clear()

    for kind, item in _docx_blocks(document):
        block_index += 1
        if kind == "paragraph":
            images += len(item._element.findall(".//" + _qn("w:drawing")))
            level = _heading_level(item)
            # Headings keep visible text only; body text keeps hyperlink targets.
            text = (item.text if level else _paragraph_text_with_links(item)).strip()
            if not text:
                continue
            if level:
                flush(block_index - 1)
                headings = {k: v for k, v in headings.items() if k < level}
                headings[level] = _single_line(text)
                continue
            rendered = _render_docx_paragraph(item, text)
        else:
            rendered = _render_docx_table(item)
            if not rendered:
                continue
        if not body:
            first_block = block_index - 1
        body.append(rendered)
    flush(block_index)

    if not sections:
        raise BoxExtractionError("DOCX contains no extractable text")
    warnings = []
    if images:
        warnings.append(f"{images} embedded image(s) in DOCX were not extracted")
    return ExtractionResult(sections, "python-docx", warnings)


def _qn(tag: str) -> str:
    from docx.oxml.ns import qn

    return qn(tag)


def _docx_blocks(document) -> Iterator[tuple[str, object]]:
    """Paragraphs and tables in body order (python-docx lists them separately)."""

    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        if child.tag == _qn("w:p"):
            yield "paragraph", Paragraph(child, document)
        elif child.tag == _qn("w:tbl"):
            yield "table", Table(child, document)


def _heading_level(paragraph) -> int:
    name = (paragraph.style.name if paragraph.style is not None else "") or ""
    if name == "Title":
        return 1
    match = re.fullmatch(r"Heading (\d)", name)
    return int(match[1]) if match else 0


def _paragraph_text_with_links(paragraph) -> str:
    """Visible text, with hyperlinks rendered as Markdown links so targets survive."""

    from docx.text.hyperlink import Hyperlink

    parts: list[str] = []
    for item in paragraph.iter_inner_content():
        if isinstance(item, Hyperlink):
            label = item.text.strip()
            url = (item.url or "").strip()
            if url and url.startswith(("http://", "https://", "mailto:")):
                parts.append(f"[{label or url}]({url})" if label != url else url)
            else:
                parts.append(item.text)  # internal bookmark links have no external target
        else:
            parts.append(item.text)
    return "".join(parts)


def _render_docx_paragraph(paragraph, text: str) -> str:
    name = (paragraph.style.name if paragraph.style is not None else "") or ""
    num_pr = paragraph._element.find(".//" + _qn("w:numPr"))
    is_list = "List" in name or num_pr is not None
    text = _escape_plain_text(text)
    if not is_list:
        return text
    level_node = num_pr.find(_qn("w:ilvl")) if num_pr is not None else None
    level = int(level_node.get(_qn("w:val"), "0")) if level_node is not None else 0
    # Bullet glyphs (not Markdown markers) keep each item an independent prose block,
    # so long lists stay splittable; "◦" marks nested items.
    return f"{'◦' if level else '•'} {text}"


def _render_docx_table(table) -> str:
    rows: list[list[str]] = []
    for row in table.rows:
        cells: list[str] = []
        previous = None
        for cell in row.cells:
            # Merged cells repeat the same underlying element; keep one copy.
            if previous is not None and cell._tc is previous:
                continue
            previous = cell._tc
            cells.append(_table_cell(cell.text))
        rows.append(cells)
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)


def _table_cell(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().replace("\\", "\\\\").replace("|", "\\|")


# ---------------------------------------------------------------- Markdown / text


def extract_markdown(path: Path, *, plain_text: bool) -> ExtractionResult:
    try:
        raw = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise BoxExtractionError("Text file is not valid UTF-8") from error
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if plain_text:
        text = _escape_plain_text(raw)
        if not text.strip():
            raise BoxExtractionError("Text file is empty")
        return ExtractionResult(
            [DocumentSection(text=text, section_reference="text:whole")], "text"
        )
    try:
        result = clean_notion_markdown(raw, remove_template_placeholders=False)
    except MarkdownExtractionError as error:
        raise BoxUnsupportedContentError(str(error)) from error
    if not result.sections:
        raise BoxExtractionError("Markdown file contains no extractable text")
    return ExtractionResult(list(result.sections), "markdown-it-py")


# ---------------------------------------------------------------- helpers

# Line starts CommonMark would treat as structure (headings, lists, quotes, HTML,
# tables, thematic breaks, setext underlines) rather than prose.
_STRUCTURAL_START = re.compile(r"^(#|>|<|\||[-+*](\s|$)|\d+[.)](\s|$)|=+\s*$|-{2,}\s*$|`{3,}|~{3,})")


def _escape_plain_text(raw: str) -> str:
    lines = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.replace("\t", " ").rstrip()
        line = line.lstrip()  # leading indentation would become a code block
        if _STRUCTURAL_START.match(line):
            line = "\\" + line if not line[0].isdigit() else re.sub(r"^(\d+)([.)])", r"\1\\\2", line)
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _single_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _page_ranges(pages: list[int]) -> str:
    ranges: list[str] = []
    start = previous = pages[0]
    for page in pages[1:] + [None]:
        if page is not None and page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        if page is not None:
            start = previous = page
    return ", ".join(ranges)


def _package_version(parser: str) -> str | None:
    package = {"pypdf": "pypdf", "python-docx": "python-docx", "markdown-it-py": "markdown-it-py"}
    name = package.get(parser)
    if name is None:
        return None
    try:
        return version(name)
    except PackageNotFoundError:
        return None
