"""Export a bounded, approved live sample for local human review.

Unlike check_notion_live.py, the output files intentionally contain real source
content. Use an ignored local directory. Existing exports are never overwritten.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
from uuid import UUID

from app.config import Settings
from app.connectors.notion import NotionConnector
from app.connectors.notion_properties import NotionSourcePolicy, lifecycle_query_filter
from app.ingestion.chunking import DocumentChunker


def markdown_label(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text.replace("\n", " "))


def render_review(documents: list[dict]) -> str:
    total = sum(len(item["chunks"]) for item in documents)
    lines = [
        "# Notion chunks — local review",
        "",
        f"{len(documents)} records · {total} chunks",
        "",
        "This is a refreshed live snapshot, regenerated after the tests. The earlier",
        "in-memory output was not saved, so this is not an archived copy of that run.",
        "Chunk text is shown verbatim inside text fences. No embeddings or database",
        "indexing were performed. This file contains organizational content; keep it local.",
        "",
        "The JSON companion contains normalized sections and complete chunk metadata.",
        "",
        "[Open machine-readable JSON](chunks.json)",
        "",
    ]
    for number, item in enumerate(documents, start=1):
        doc = item["document"]
        lines += [
            f"## {number}. {markdown_label(doc['title'])}",
            "",
            f"Source: <{doc['source_url']}>",
            "",
            f"Kind: `{doc['metadata']['document_kind']}` · Record ID: `{doc['external_id']}`",
            "",
            f"Source version: `{doc['source_version']}`",
            "",
        ]
        for chunk in item["chunks"]:
            roles = sorted(
                {doc["sections"][ref["section_index"]]["kind"] for ref in chunk["references"]}
            )
            role = (
                "properties only"
                if roles == ["properties"]
                else "body + properties"
                if len(roles) > 1
                else "body"
            )
            lines += [
                f"### Chunk {chunk['chunk_index'] + 1} — {chunk['token_count']} tokens ({role})",
                "",
            ]
            for ref in chunk["references"]:
                heading = " > ".join(ref["heading_path"]) or "Page body"
                lines.append(f"- {markdown_label(heading)} · `{ref['section_reference']}`")
            runs = [len(match[0]) for match in re.finditer(r"`+", chunk["content"])]
            fence = "`" * max(3, 1 + max(runs, default=0))
            lines += ["", fence + "text", chunk["content"], fence, ""]
    return "\n".join(lines)


def save_review(directory: Path, documents: list[dict]) -> None:
    """Create a fresh private export directory; refuse all existing targets."""

    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=False)
    outputs = {
        "chunks.md": render_review(documents),
        "chunks.json": json.dumps(
            {
                "snapshot": "refreshed_live_sample",
                "documents": documents,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    }
    for name, text in outputs.items():
        descriptor = os.open(directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)


async def collect_review(
    settings: Settings, source_ids: list[str], expected: set[str]
) -> list[dict]:
    sources = list(dict.fromkeys(str(UUID(value)) for value in source_ids))
    if not sources or len(sources) > 4:
        raise ValueError("Select between one and four approved data sources")
    allowed = {str(UUID(value)) for value in settings.notion_allowed_data_source_ids}
    if set(sources) - allowed or settings.notion_api_token is None:
        raise ValueError("Token and explicit data-source approvals are required")
    policies = {
        str(UUID(key)): value
        for key, value in settings.notion_source_policies.items()
        if str(UUID(key)) in sources
    }
    async with NotionConnector(
        token=settings.notion_api_token.get_secret_value(),
        allowed_page_ids=[],
        allowed_data_source_ids=sources,
        source_policies=policies,
        api_version=settings.notion_api_version,
    ) as connector:
        pages: list[str] = []
        for source_id in sources:
            schema = await connector.fetch_notion_data_source(source_id)
            policy = policies.get(source_id, NotionSourcePolicy())
            sample = await connector.sample_notion_pages(
                source_id,
                limit=5,
                query_filter=lifecycle_query_filter(schema, policy),
            )
            pages.extend(str(UUID(row["id"])) for row in sample.pages)
        pages = list(dict.fromkeys(pages))
        if expected and set(pages) != expected:
            raise ValueError("The sampled record set changed; no review files were written")
        chunker = DocumentChunker()
        documents = []
        for page_id in pages:
            doc = await connector.fetch_and_normalize_page(page_id)
            chunks = chunker.chunk_document(doc)
            if not chunks or any(
                chunk.token_count != chunker.count_tokens(chunk.content)
                or chunk.token_count > chunker.config.max_tokens
                for chunk in chunks
            ):
                raise ValueError("Chunk validation failed; no review files were written")
            documents.append(
                {
                    "document": doc.model_dump(mode="json"),
                    "chunks": [chunk.model_dump(mode="json") for chunk in chunks],
                }
            )
        return documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-source-id", action="append", required=True)
    parser.add_argument("--expected-page-id", action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        print(json.dumps({"status": "blocked", "reason": "output_directory_already_exists"}))
        return 2
    try:
        expected = {str(UUID(value)) for value in args.expected_page_id}
        documents = asyncio.run(collect_review(Settings(), args.data_source_id, expected))
        save_review(args.output_dir, documents)
    except Exception as error:
        # Exported content stays in files; API/validation messages stay out of logs.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 2
    print(
        json.dumps(
            {
                "status": "exported",
                "records": len(documents),
                "chunks": sum(len(doc["chunks"]) for doc in documents),
                "markdown": str(args.output_dir.resolve() / "chunks.md"),
                "json": str(args.output_dir.resolve() / "chunks.json"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
