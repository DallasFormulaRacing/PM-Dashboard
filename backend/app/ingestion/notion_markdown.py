"""Conservative cleanup and heading sections for Notion enhanced Markdown v1.

Parse source spans without rendering HTML or following links. CommonMark handles
code, lists and pipe tables; Notion containers remain indivisible blocks.
"""

from dataclasses import dataclass
import re

from markdown_it import MarkdownIt

from app.domain.documents import DocumentSection


PARSER = MarkdownIt("commonmark").enable("table")
PROTECTED = re.compile(
    r"^\s*<(callout|details|table|ul|ol|synced_block|synced_block_reference)\b[^>]*>"
)
LAYOUT = re.compile(r"^<(\/?)(columns|column)\b[^>]*>\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
EMPTY_ITEM = re.compile(r"\s*(?:[-+*]|\d+[.)])\s*(?:\[[ xX]\])?\s*")
EMPTY_BLOCK = re.compile(r"<(?:p|empty-block|pdf|image|audio|video|file)\s*/>")
MEDIA = re.compile(r"<(?:pdf|image|audio|video|file)\b[^>]*(?:src|source)=|!\[[^\]]*\]\(")
PLACEHOLDERS = frozenset(
    {
        "Provide an overview of the task and related details.",
        "Provide an overview of the project and related details.",
    }
)


class MarkdownExtractionError(ValueError):
    """Content cannot be normalized with trustworthy structural boundaries."""


@dataclass(frozen=True)
class MarkdownBlock:
    text: str
    kind: str
    start: int  # zero-based inclusive source line
    end: int  # exclusive source line
    heading_level: int = 0
    heading: str = ""


@dataclass(frozen=True)
class MarkdownCleanupResult:
    sections: tuple[DocumentSection, ...]
    removed_placeholders: int
    removed_empty_items: int
    removed_empty_blocks: int

    def metadata(self) -> dict[str, str | int]:
        return {
            "version": "notion-cleanup-v1",
            "removed_placeholders": self.removed_placeholders,
            "removed_empty_items": self.removed_empty_items,
            "removed_empty_blocks": self.removed_empty_blocks,
        }


def _flatten_layout(lines: list[str]) -> list[str]:
    """Remove presentation wrappers while retaining original line positions."""

    output: list[str] = []
    layout_depth = 0
    fence: str | None = None
    for original in lines:
        line = original
        for _ in range(layout_depth):
            if line.startswith("\t"):
                line = line[1:]
            elif line.startswith("    "):
                line = line[4:]
            else:
                break
        marker = FENCE.match(line)
        if fence is not None:
            output.append(line)
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                fence = None
            continue
        if marker:
            fence = marker[1]
            output.append(line)
            continue
        wrapper = LAYOUT.fullmatch(line)
        if wrapper:
            layout_depth += -1 if wrapper[1] else 1
            if layout_depth < 0:
                raise MarkdownExtractionError("Unbalanced Notion layout wrapper")
            output.append("")
        elif re.fullmatch(r"</?p(?:\s+[^>]*)?>\s*", line):
            output.append("")
        else:
            output.append(line)
    if layout_depth:
        raise MarkdownExtractionError("Unclosed Notion layout wrapper")
    return output


def markdown_blocks(text: str) -> list[MarkdownBlock]:
    """Get source-preserving root blocks; never split inside a container."""

    lines = text.splitlines()
    initial_tokens = PARSER.parse(text)
    code_lines = {
        index
        for token in initial_tokens
        if token.type in {"fence", "code_block"} and token.map
        for index in range(*token.map)
    }
    protected: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        match = PROTECTED.match(lines[index]) if index not in code_lines else None
        if not match or lines[index].rstrip().endswith("/>"):
            index += 1
            continue
        tag = match[1]
        start = index
        depth = 0
        inner_fence: str | None = None
        while index < len(lines):
            stripped = lines[index].lstrip(" \t")
            marker = FENCE.match(stripped)
            if inner_fence is not None:
                if re.fullmatch(
                    rf"{re.escape(inner_fence[0])}{{{len(inner_fence)},}}\s*", stripped
                ):
                    inner_fence = None
            elif marker:
                inner_fence = marker[1]
            else:
                for part in re.finditer(rf"<(\/?)({tag})\b[^>]*>", lines[index]):
                    if not part[0].endswith("/>"):
                        depth += -1 if part[1] else 1
            index += 1
            if depth == 0:
                break
        if depth:
            raise MarkdownExtractionError(f"Unclosed Notion {tag} container")
        protected.append((start, index))

    blocks: list[MarkdownBlock] = []

    def parse_segment(start: int, end: int) -> None:
        segment = "\n".join(lines[start:end])
        tokens = PARSER.parse(segment)
        cursor = start
        for pos, token in enumerate(tokens):
            if token.level != 0 or token.map is None or token.nesting < 0:
                continue
            first, last = (value + start for value in token.map)
            if first < cursor:
                continue
            # Markdown reference definitions have no rendered token; retain them.
            gap = "\n".join(lines[cursor:first]).strip()
            if gap:
                blocks.append(MarkdownBlock(gap, "structured", cursor, first))
            raw = "\n".join(lines[first:last]).strip("\n")
            kind = {
                "heading_open": "heading",
                "paragraph_open": "prose",
                "table_open": "table",
                "fence": "code",
                "code_block": "code",
                "hr": "divider",
            }.get(token.type, "structured")
            title = tokens[pos + 1].content if kind == "heading" else ""
            # Color/toggle attributes are presentation, not heading text.
            title = re.sub(r'\s+\{(?:color|toggle)="[^}]*\}\s*$', "", title)
            blocks.append(
                MarkdownBlock(
                    raw,
                    kind,
                    first,
                    last,
                    int(token.tag[1:]) if kind == "heading" else 0,
                    title,
                )
            )
            cursor = last
        remainder = "\n".join(lines[cursor:end]).strip()
        if remainder:
            blocks.append(MarkdownBlock(remainder, "structured", cursor, end))

    cursor = 0
    for start, end in protected:
        parse_segment(cursor, start)
        kind = "table" if lines[start].lstrip().startswith("<table") else "structured"
        blocks.append(MarkdownBlock("\n".join(lines[start:end]), kind, start, end))
        cursor = end
    parse_segment(cursor, len(lines))
    return blocks


def _remove_empty_items(text: str) -> tuple[str, int]:
    lines = text.splitlines()
    count = 0
    while True:
        empty_lines: set[int] = set()
        for token in PARSER.parse("\n".join(lines)):
            if token.type == "list_item_open" and token.map:
                start, end = token.map
                if EMPTY_ITEM.fullmatch("\n".join(lines[start:end])):
                    empty_lines.update(range(start, end))
                    count += 1
        if not empty_lines:
            return "\n".join(lines), count
        lines = [line for index, line in enumerate(lines) if index not in empty_lines]


def clean_notion_markdown(
    markdown: str,
    *,
    remove_template_placeholders: bool = True,
) -> MarkdownCleanupResult:
    lines = _flatten_layout(markdown.splitlines())
    sections: list[DocumentSection] = []
    headings: dict[int, str] = {}
    body: list[str] = []
    first_line = last_line = 0
    removed_placeholders = removed_items = removed_blocks = 0

    def flush() -> None:
        if body:
            sections.append(
                DocumentSection(
                    text="\n\n".join(body),
                    heading_path=[headings[level] for level in sorted(headings)],
                    section_reference=f"markdown:lines:{first_line + 1}-{last_line}",
                )
            )
        body.clear()

    for block in markdown_blocks("\n".join(lines)):
        if block.kind == "heading":
            flush()
            headings = {
                level: title for level, title in headings.items() if level < block.heading_level
            }
            headings[block.heading_level] = block.heading
            continue
        if block.kind == "divider":
            removed_blocks += 1
            continue
        text = block.text
        if block.kind != "code":
            probe = text
            has_image = False
            if block.kind == "prose":
                children = PARSER.parseInline(text)[0].children or []
                probe = "\n".join(
                    child.content for child in children if child.type == "html_inline"
                )
                has_image = any(child.type == "image" for child in children)
            if re.search(r"<unknown\b", probe):
                raise MarkdownExtractionError("Unresolved Notion block remains in Markdown")
            if has_image or MEDIA.search(probe):
                raise MarkdownExtractionError(
                    "Attachment/media content requires a separate approved extraction step"
                )
            if EMPTY_BLOCK.fullmatch(text.strip()):
                removed_blocks += 1
                continue
            if (
                block.kind == "prose"
                and remove_template_placeholders
                and text.strip() in PLACEHOLDERS
            ):
                removed_placeholders += 1
                continue
            if block.kind == "structured" and re.match(r"\s*(?:[-+*]|\d+[.)])\s", text):
                text, removed = _remove_empty_items(text)
                removed_items += removed
        if not text.strip():
            continue
        if not body:
            first_line = block.start
        last_line = block.end
        body.append(text.strip("\n"))
    flush()
    return MarkdownCleanupResult(
        tuple(sections), removed_placeholders, removed_items, removed_blocks
    )
