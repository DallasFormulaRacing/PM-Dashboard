"""Tests for the common source-document normalization contract."""

import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from app.domain.documents import DocumentSection, NormalizedDocument


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def load_document_fixture(relative_path: str) -> NormalizedDocument:
    payload = json.loads((FIXTURE_ROOT / relative_path).read_text(encoding="utf-8"))
    return NormalizedDocument.model_validate(payload)


@pytest.mark.parametrize(
    ("fixture_path", "expected_source"),
    [
        ("notion/announcement.json", "notion"),
        ("box/extracted_pdf.json", "box"),
    ],
)
def test_source_fixtures_share_one_contract(
    fixture_path: str,
    expected_source: str,
) -> None:
    document = load_document_fixture(fixture_path)

    assert isinstance(document, NormalizedDocument)
    assert document.source == expected_source
    assert document.title
    assert document.source_url is not None
    assert all(isinstance(section, DocumentSection) for section in document.sections)


def test_notion_announcement_preserves_heading_and_url() -> None:
    document = load_document_fixture("notion/announcement.json")

    assert document.title == "Synthetic Program Announcement"
    assert document.sections[0].heading_path == ["Announcements", "Mentoring Program"]
    assert document.sections[0].page_number is None
    assert document.source_url.startswith("https://www.notion.so/")


def test_extracted_pdf_preserves_pages_and_metadata() -> None:
    document = load_document_fixture("box/extracted_pdf.json")

    assert document.title == "Synthetic Benefits Guide.pdf"
    assert [section.page_number for section in document.sections] == [1, 3]
    assert document.sections[1].heading_path == ["Support"]
    assert document.metadata["mime_type"] == "application/pdf"


@pytest.mark.parametrize(
    "sections",
    [
        [],
        [{"text": "   "}],
    ],
)
def test_empty_documents_are_rejected(sections: list[dict[str, str]]) -> None:
    with pytest.raises(ValidationError):
        NormalizedDocument(
            source="notion",
            external_id="page-1",
            title="Empty fixture",
            sections=sections,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "sharepoint"),
        ("source_url", "not-a-url"),
        ("modified_at", "2026-10-22T16:50:00"),
    ],
)
def test_invalid_document_metadata_is_visible(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "source": "box",
        "external_id": "file-1",
        "title": "Synthetic file",
        "source_url": "https://app.box.com/file/file-1",
        "modified_at": "2026-10-22T16:50:00-05:00",
        "sections": [{"text": "Extracted fixture text."}],
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        NormalizedDocument.model_validate(payload)


def test_metadata_must_be_json_safe() -> None:
    with pytest.raises(ValidationError):
        NormalizedDocument(
            source="box",
            external_id="file-1",
            title="Synthetic file",
            sections=[DocumentSection(text="Extracted fixture text.")],
            metadata={"unsupported": object()},
        )


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unexpected_field"):
        NormalizedDocument.model_validate(
            {
                "source": "notion",
                "external_id": "page-1",
                "title": "Synthetic page",
                "sections": [{"text": "Fixture text."}],
                "unexpected_field": "would otherwise be silently discarded",
            }
        )


def test_document_round_trips_through_json() -> None:
    original = load_document_fixture("box/extracted_pdf.json")

    restored = NormalizedDocument.model_validate_json(original.model_dump_json())

    assert restored == original
