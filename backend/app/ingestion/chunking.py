"""Deterministic heading-first chunks with measured final-content token limits."""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator
import tiktoken

from app.domain.chunks import ChunkReference, DocumentChunk
from app.domain.documents import DocumentSection, NormalizedDocument
from app.ingestion.notion_markdown import MarkdownExtractionError, markdown_blocks


class ChunkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_tokens: int = Field(default=500, strict=True, ge=1)
    max_tokens: int = Field(default=700, strict=True, ge=1)
    announcement_tokens: int = Field(default=400, strict=True, ge=1)
    overlap_tokens: int = Field(default=50, strict=True, ge=0)
    tokenizer: str = "cl100k_base"

    @model_validator(mode="after")
    def validate_limits(self) -> ChunkingConfig:
        if self.target_tokens > self.max_tokens or self.announcement_tokens > self.max_tokens:
            raise ValueError("Target and announcement limits must not exceed max_tokens")
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("Overlap must be smaller than target_tokens")
        return self


class ChunkingError(ValueError):
    """Document cannot be chunked without losing or breaking structured evidence."""


class DocumentChunker:
    """Local tokenizer only; no provider API calls or database writes."""

    version = "heading-first-v1"

    def __init__(self, config: ChunkingConfig | None = None) -> None:
        self.config = config or ChunkingConfig()
        self.encoding = tiktoken.get_encoding(self.config.tokenizer)

    def count_tokens(self, text: str) -> int:
        # Retrieved content is data, including strings resembling special tokens.
        return len(self.encoding.encode(text, disallowed_special=()))

    def chunk_document(self, document: NormalizedDocument) -> list[DocumentChunk]:
        if document.metadata.get("document_kind") == "hub":
            return []
        context = [f"Title: {document.title}"]
        path = document.metadata.get("ancestor_path")
        if isinstance(path, list) and path and all(isinstance(part, str) for part in path):
            context.append("Source path: " + " > ".join(path))
        properties = document.metadata.get("properties")
        date = properties.get("date") if isinstance(properties, dict) else None
        if isinstance(date, dict) and isinstance(date.get("start"), str):
            context.append("Record date: " + date["start"])
            if date.get("end"):
                context.append("Record date end: " + str(date["end"]))
            if date.get("time_zone"):
                context.append("Record timezone: " + str(date["time_zone"]))
        prefix = "\n".join(context) + "\n\n"

        def render(section: DocumentSection, text: str) -> str:
            heading = " > ".join(section.heading_path)
            return (f"## {heading}\n\n" if heading else "") + text.strip("\n")

        def reference(index: int) -> ChunkReference:
            section = document.sections[index]
            return ChunkReference(
                section_index=index,
                heading_path=section.heading_path,
                page_number=section.page_number,
                section_reference=section.section_reference,
            )

        chunks: list[DocumentChunk] = []

        def emit(content: str, indices: list[int]) -> None:
            content = content.rstrip()
            tokens = self.count_tokens(content)
            if tokens > self.config.max_tokens:
                raise ChunkingError("Rendered chunk exceeds max_tokens")
            refs = [reference(index) for index in dict.fromkeys(indices)]
            identity = {
                "source": document.source,
                "external_id": document.external_id,
                "content": content,
                "references": [ref.model_dump() for ref in refs],
                "config": self.config.model_dump(),
                "version": self.version,
            }
            digest = hashlib.sha256(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            chunks.append(
                DocumentChunk(
                    source=document.source,
                    external_id=document.external_id,
                    title=document.title,
                    source_url=document.source_url,
                    chunk_index=len(chunks),
                    content=content,
                    token_count=tokens,
                    content_hash=digest,
                    references=refs,
                    tokenizer=self.config.tokenizer,
                    chunking_version=self.version,
                )
            )

        whole = prefix + "\n\n".join(render(section, section.text) for section in document.sections)
        kind = document.metadata.get("document_kind")
        compact_limit = (
            self.config.announcement_tokens
            if kind == "announcement"
            else self.config.max_tokens
            if kind in {"task", "timeline_item"}
            else 0
        )
        # Never join source pages merely because their text is short.
        same_page = len({section.page_number for section in document.sections}) == 1
        if compact_limit and same_page and self.count_tokens(whole) <= compact_limit:
            emit(whole, list(range(len(document.sections))))
            return chunks

        for index, section in enumerate(document.sections):
            heading = render(section, "")

            def fits(text: str, limit: int) -> bool:
                return self.count_tokens(prefix + render(section, text)) <= limit

            pending: list[str] = []

            def flush() -> None:
                if pending:
                    emit(prefix + render(section, "\n\n".join(pending)), [index])
                    pending.clear()

            try:
                blocks = markdown_blocks(section.text)
            except MarkdownExtractionError as error:
                raise ChunkingError(str(error)) from error
            # Property facts form a record; avoid splitting individual fields or people lists.
            if section.kind == "properties":
                if not fits(section.text, self.config.max_tokens):
                    raise ChunkingError("Record properties exceed max_tokens; revise source policy")
                emit(prefix + render(section, section.text), [index])
                continue
            for block in blocks:
                text = block.text.strip("\n")
                if not text:
                    continue
                if fits(text, self.config.max_tokens):
                    combined = "\n\n".join([*pending, text])
                    if pending and not fits(combined, self.config.target_tokens):
                        flush()
                    pending.append(text)
                    continue
                flush()
                if block.kind == "prose":
                    for part in self._split_prose(text, prefix + heading):
                        emit(prefix + render(section, part), [index])
                elif block.kind == "table":
                    for part in self._split_table(text, prefix + heading):
                        emit(prefix + render(section, part), [index])
                else:
                    raise ChunkingError(
                        f"Structured {block.kind} block exceeds max_tokens in section {index}"
                    )
            flush()
        return chunks

    def _prefix_end(self, text: str, prefix: str, limit: int) -> int:
        """Find a fitting character boundary; never decode partial UTF-8 tokens."""

        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.count_tokens(prefix + text[:middle]) <= limit:
                low = middle
            else:
                high = middle - 1
        return low

    def _split_prose(self, text: str, prefix: str) -> list[str]:
        pieces: list[str] = []
        start = 0
        while start < len(text):
            remaining = text[start:]
            end = self._prefix_end(remaining, prefix, self.config.target_tokens)
            if end == 0:
                raise ChunkingError("Title/heading context leaves no prose token budget")
            if end < len(remaining):
                boundaries = [
                    match.end()
                    for match in re.finditer(r"[.!?](?=\s)|\s+", remaining[:end])
                    if match.end() >= end // 2
                ]
                if boundaries:
                    end = boundaries[-1]
            fragment = remaining[:end]
            pieces.append(fragment)
            if end == len(remaining):
                break
            overlap = self.config.overlap_tokens
            suffix_start = len(fragment)
            if overlap:
                # Find a suffix within the requested overlap token budget.
                low, high = 0, len(fragment)
                while low < high:
                    middle = (low + high) // 2
                    if self.count_tokens(fragment[middle:]) <= overlap:
                        high = middle
                    else:
                        low = middle + 1
                suffix_start = low
                while suffix_start < len(fragment) and not fragment[suffix_start].isspace():
                    suffix_start += 1
            start += max(1, suffix_start)
        return pieces

    def _split_table(self, text: str, prefix: str) -> list[str]:
        if text.lstrip().startswith("<table"):
            matches = list(re.finditer(r"<tr\b[^>]*>.*?</tr>", text, re.DOTALL))
            if not matches or any(
                text[a.end() : b.start()].strip() for a, b in zip(matches, matches[1:])
            ):
                raise ChunkingError("Cannot identify intact Notion table rows")
            header = [text[: matches[0].start()].strip()]
            tail = text[matches[-1].end() :].strip()
            data_rows = [match[0] for match in matches]
            if re.search(r'\bheader-row=["\']true["\']', header[0]):
                header.append(data_rows.pop(0))
        else:
            lines = text.splitlines()
            header, data_rows, tail = lines[:2], lines[2:], ""

        def render(rows: list[str]) -> str:
            return "\n".join([*header, *rows, tail]).strip()

        result: list[str] = []
        rows: list[str] = []
        for row in data_rows:
            candidate = render([*rows, row])
            if rows and self.count_tokens(prefix + candidate) > self.config.target_tokens:
                result.append(render(rows))
                rows = []
            if self.count_tokens(prefix + render([row])) > self.config.max_tokens:
                raise ChunkingError("Table header and single row exceed max_tokens")
            rows.append(row)
        if rows:
            result.append(render(rows))
        if not result:
            raise ChunkingError("Table header exceeds max_tokens")
        return result


def chunk_document(
    document: NormalizedDocument,
    config: ChunkingConfig | None = None,
) -> list[DocumentChunk]:
    return DocumentChunker(config).chunk_document(document)
