"""Offline connector-to-chunks checks through synthetic HTTP responses."""

import asyncio
import importlib.util
from pathlib import Path
import socket

import pytest


PREVIEW_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "preview_notion_fixture.py"
spec = importlib.util.spec_from_file_location("fixture_preview", PREVIEW_SCRIPT)
preview_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview_module)


@pytest.mark.parametrize("example", ["task", "meeting", "technical"])
def test_normalize_cleanup_and_chunk_fixture_without_network(monkeypatch, example) -> None:
    def deny_network(*args, **kwargs):
        raise AssertionError("Fixture preview must not open network connections")

    # Warm the public tokenizer vocabulary before disabling network. This sends
    # no document text; subsequent fixture processing must be entirely local.
    preview_module.DocumentChunker()
    monkeypatch.setattr(socket, "create_connection", deny_network)
    result = asyncio.run(preview_module.preview("In Progress", example))
    assert len(result["documents"]) == 1
    assert result["chunks"]
    assert not result["skipped"]
    for chunk in result["chunks"]:
        assert 0 < chunk["token_count"] <= 700
        assert chunk["source_url"] == result["documents"][0]["source_url"]
        assert all(ref["section_reference"] for ref in chunk["references"])
    content = "\n".join(chunk["content"] for chunk in result["chunks"])
    assert "Provide an overview of the task and related details." not in content
    if example == "task":
        assert len(result["chunks"]) == 1
        assert "Review TEST-42" in content and "Synthetic Engineer" in content
        cleanup = result["documents"][0]["metadata"]["notion_cleanup"]
        assert cleanup["removed_placeholders"] == 1
        assert cleanup["removed_empty_items"] == 1
        assert cleanup["removed_empty_blocks"] == 1
    elif example == "meeting":
        assert len(result["chunks"]) >= 3
        assert all("Record date: 2026-10-20" in chunk["content"] for chunk in result["chunks"])
        assert "Attendees: Synthetic Engineer" in content
    else:
        assert len(result["chunks"]) > 4
        assert "CAPTURE-END" in content and "校准" in content


def test_inactive_fixture_produces_no_chunks() -> None:
    result = asyncio.run(preview_module.preview("Draft"))
    assert result["documents"] == result["chunks"] == []
    assert result["skipped"][0]["reason"] == "unpublished_or_inactive_status"
