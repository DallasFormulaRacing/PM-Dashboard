"""Read-only live check of approved Box files with content-free JSON output.

Run from backend/: python scripts/check_box_live.py [--file-id ID ...] [--export]
For each allowlisted file: metadata → policy → download → extract → chunk, twice,
to verify determinism. Prints only counts, codes, and file names. `--export` writes
real chunk content to the git-ignored backend/.local/box-chunk-review/ for review;
never commit it. No embedding, generation, or database writes happen here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.connectors.box import (  # noqa: E402
    BOX_WEB_FILE_URL,
    BoxAPIError,
    BoxConnector,
    BoxConnectorError,
    BoxFileExcludedError,
    BoxIncompleteContentError,
)
from app.connectors.box_auth import BoxAuthError  # noqa: E402
from app.ingestion.chunking import ChunkingError, DocumentChunker  # noqa: E402

EXPORT_DIR = Path(__file__).resolve().parents[1] / ".local" / "box-chunk-review"


async def check_file(
    connector: BoxConnector, chunker: DocumentChunker, file_id: str, *, export: bool = False
) -> dict:
    report: dict = {"file_id": file_id}
    try:
        metadata = await connector.fetch_box_metadata(file_id)
    except (BoxConnectorError, BoxAuthError) as error:
        return report | _failure("metadata", error)
    report |= {
        "name": metadata.name,
        "extension": metadata.extension,
        "size_bytes": metadata.size,
        "has_version_id": metadata.version_id is not None,
        "has_sha1": metadata.sha1 is not None,
    }
    reason = connector.exclusion_reason(metadata)
    if reason:
        return report | {"outcome": "excluded", "reason": reason, "content_requested": False}

    runs = []
    for _ in range(2):
        try:
            document = await connector.fetch_and_normalize_file(file_id)
            chunks = chunker.chunk_document(document)
        except BoxIncompleteContentError as error:
            return report | {"outcome": "rejected", "reason": str(error)}
        except BoxFileExcludedError as error:
            return report | {"outcome": "excluded", "reason": error.reason}
        except ChunkingError as error:
            return report | {"outcome": "chunking_failed", "reason": str(error)}
        except (BoxConnectorError, BoxAuthError) as error:
            return report | _failure("download/extract", error)
        runs.append((document, chunks))

    (document, chunks), (_, chunks_again) = runs
    sections = document.sections
    report |= {
        "outcome": "processed",
        "canonical_url_ok": document.source_url == BOX_WEB_FILE_URL.format(file_id=file_id),
        "version_matches_metadata": document.source_version == metadata.version_id,
        "box_account": document.metadata.get("box_account"),
        "parser": document.metadata.get("parser"),
        "sections": len(sections),
        "sections_with_page_number": sum(s.page_number is not None for s in sections),
        "sections_with_headings": sum(bool(s.heading_path) for s in sections),
        "chunks": len(chunks),
        "chunk_tokens": [chunk.token_count for chunk in chunks],
        "max_chunk_tokens": max((chunk.token_count for chunk in chunks), default=0),
        "chunks_with_page_reference": sum(
            any(ref.page_number for ref in chunk.references) for chunk in chunks
        ),
        "extraction_warnings": document.metadata.get("extraction_warnings", []),
        "deterministic": [c.content_hash for c in chunks]
        == [c.content_hash for c in chunks_again],
    }
    if chunks and export:
        _export(document, chunks)
        report["exported_to"] = str(EXPORT_DIR / f"{file_id}.json")
    return report


def _failure(stage: str, error: Exception) -> dict:
    result = {"outcome": "error", "stage": stage, "error": type(error).__name__}
    if isinstance(error, BoxAPIError):
        result |= {"status_code": error.status_code, "code": error.code}
    else:
        result["detail"] = str(error)
    return result


def _export(document, chunks) -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "warning": "Real organizational content. Git-ignored. Do not commit or share.",
        "document": document.model_dump(mode="json", exclude={"sections"}),
        "chunks": [chunk.model_dump(mode="json") for chunk in chunks],
    }
    path = EXPORT_DIR / f"{document.external_id}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-id", action="append", help="limit to these allowlisted IDs")
    parser.add_argument("--export", action="store_true", help="write chunks to .local/")
    args = parser.parse_args()

    settings = Settings()
    chunker = DocumentChunker()
    async with BoxConnector.from_settings(settings) as connector:
        file_ids = args.file_id or connector.allowed_file_ids
        reports = [
            await check_file(connector, chunker, file_id, export=args.export)
            for file_id in file_ids
        ]
    for report in reports:
        print(json.dumps(report, ensure_ascii=False))
    outcomes: dict[str, int] = {}
    for report in reports:
        outcomes[report["outcome"]] = outcomes.get(report["outcome"], 0) + 1
    print(json.dumps({"summary": outcomes, "files": len(reports)}))


if __name__ == "__main__":
    asyncio.run(main())
