"""Explicit review exports preserve exact chunk text and never overwrite files."""

import asyncio
import importlib.util
import json
from pathlib import Path
import stat

import pytest

from app.config import Settings
from app.domain.documents import DocumentSection, NormalizedDocument
from app.ingestion.chunking import DocumentChunker


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/export_notion_chunks.py"
SPEC = importlib.util.spec_from_file_location("notion_review_export", SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


def synthetic_document():
    doc = NormalizedDocument(
        source="notion",
        external_id="synthetic-review",
        title="Synthetic <review>",
        source_url="https://www.notion.so/synthetic",
        source_version="fixture-v1",
        sections=[
            DocumentSection(
                text="Literal fixture content.\n```\n# Not a review heading\n```",
                section_reference="fixture:body",
            )
        ],
        metadata={"document_kind": "technical_doc"},
    )
    chunks = DocumentChunker().chunk_document(doc)
    return {
        "document": doc.model_dump(mode="json"),
        "chunks": [c.model_dump(mode="json") for c in chunks],
    }


def test_review_contains_exact_chunk_text_source_and_safe_fences():
    item = synthetic_document()
    text = exporter.render_review([item])
    assert item["chunks"][0]["content"] in text
    assert "https://www.notion.so/synthetic" in text
    assert "````text" in text
    assert "refreshed live snapshot" in text
    assert "Synthetic \\<review\\>" in text


def test_local_export_is_private_and_refuses_existing_directory(tmp_path):
    target = tmp_path / "review"
    item = synthetic_document()
    exporter.save_review(target, [item])
    payload = json.loads((target / "chunks.json").read_text())
    assert payload["documents"][0]["chunks"][0]["content"] == item["chunks"][0]["content"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((target / "chunks.md").stat().st_mode) == 0o600
    before = (target / "chunks.md").read_text()
    with pytest.raises(FileExistsError):
        exporter.save_review(target, [])
    assert (target / "chunks.md").read_text() == before


def test_export_requires_approved_sources_before_network(monkeypatch):
    def disallow(**kwargs):
        raise AssertionError("Unapproved source must not initialize a connector")

    monkeypatch.setattr(exporter, "NotionConnector", disallow)
    settings = Settings(
        _env_file=None,
        NOTION_API_TOKEN="unit-test-token",
        NOTION_ALLOWED_DATA_SOURCE_IDS=[],
        NOTION_SOURCE_POLICIES={},
    )
    with pytest.raises(ValueError, match="explicit data-source approvals"):
        asyncio.run(
            exporter.collect_review(
                settings,
                ["33333333-3333-4333-8333-333333333333"],
                set(),
            )
        )
