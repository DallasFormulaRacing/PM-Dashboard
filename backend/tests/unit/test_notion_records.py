"""Synthetic workspace-shaped records and source-policy enforcement."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError
import pytest

from app.config import Settings
from app.connectors.notion import (
    NotionConnector,
    NotionIncompleteQueryError,
    NotionRecordExcludedError,
    NotionResponseError,
    NotionSourceNotAllowedError,
)
from app.connectors.notion_properties import NotionSourcePolicy


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "notion"
PAGE_ID = "44444444-4444-4444-8444-444444444444"
OTHER_ID = "77777777-7777-4777-8777-777777777777"
SOURCE_ID = "33333333-3333-4333-8333-333333333333"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def normalize(page: dict[str, Any], *, policy: NotionSourcePolicy | None = None):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/markdown"):
            return httpx.Response(200, json=fixture("task_markdown_response.json"))
        return httpx.Response(200, json=page)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with NotionConnector(
                token="synthetic", allowed_page_ids=[page["id"]], client=client,
                source_policies={page["id"]: policy} if policy else {},
            ) as connector:
                return await connector.fetch_and_normalize_page(page["id"])

    return asyncio.run(scenario()), requests


@pytest.mark.parametrize("status", [
    "Published", "Not started", "Not Started", "In progress", "In Progress", "Waiting",
])
def test_active_status_variants_are_included(status: str) -> None:
    page = fixture("task_page_response.json")
    page["properties"]["Status"]["status"]["name"] = status
    doc, _ = normalize(page)
    assert doc.metadata["lifecycle_policy"] == "published_or_active"
    assert doc.metadata["properties"]["status"] == status.lower()


@pytest.mark.parametrize("status", [
    "Draft", "In Review", "Done", "Completed", "Outdated", "Cancelled", "unknown", "", None,
])
def test_ineligible_status_never_fetches_markdown(status: str | None) -> None:
    page = fixture("task_page_response.json")
    page["properties"]["Status"]["status"] = (
        {"name": status} if status is not None else None
    )
    paths: list[str] = []

    def handler(request: httpx.Request):
        paths.append(request.url.path)
        return httpx.Response(200, json=page)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = NotionConnector(
                token="synthetic", allowed_page_ids=[PAGE_ID], client=client,
            )
            with pytest.raises(NotionRecordExcludedError) as error:
                await connector.fetch_and_normalize_page(PAGE_ID)
            assert error.value.reason == "unpublished_or_inactive_status"

    asyncio.run(scenario())
    assert paths == [f"/v1/pages/{PAGE_ID}"]


@pytest.mark.parametrize("flag", ["archived", "in_trash"])
def test_archived_and_trashed_records_are_excluded(flag: str) -> None:
    page = fixture("task_page_response.json")
    page[flag] = True
    with pytest.raises(NotionRecordExcludedError, match=flag):
        normalize(page)


def test_statusless_record_is_implicitly_active() -> None:
    page = fixture("task_page_response.json")
    del page["properties"]["Status"]
    doc, _ = normalize(page)
    assert doc.metadata["lifecycle_policy"] == "implicit_active"
    assert "status" not in doc.metadata["properties"]


def test_properties_are_searchable_without_copying_personal_or_file_payloads() -> None:
    doc, _ = normalize(fixture("task_page_response.json"))
    evidence = "\n".join(section.text for section in doc.sections)
    assert "TEST-42" in evidence
    assert "Synthetic Engineer" in evidence
    assert "66666666-6666-4666-8666-666666666666" in evidence
    assert "2026-11-04" in evidence
    assert doc.metadata["document_kind"] == "task"
    assert doc.metadata["properties"]["assignees"] == [{
        "id": "66666666-6666-4666-8666-666666666666", "name": "Synthetic Engineer",
    }]
    serialized = doc.model_dump_json()
    for omitted in ("synthetic@example", "omit@example", "avatar.png", "unapproved.pdf"):
        assert omitted not in serialized
    assert doc.metadata["data_source_id"] == SOURCE_ID
    assert doc.metadata["parent_type"] == "data_source_id"
    assert doc.metadata["parent_id"] == SOURCE_ID


def test_property_only_record_is_a_document() -> None:
    page = fixture("task_page_response.json")

    def handler(request):
        if request.url.path.endswith("/markdown"):
            payload = fixture("task_markdown_response.json")
            payload["markdown"] = ""
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=page)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = NotionConnector(
                token="synthetic", allowed_page_ids=[PAGE_ID], client=client,
            )
            return await connector.fetch_and_normalize_page(PAGE_ID)

    doc = asyncio.run(scenario())
    assert len(doc.sections) == 1
    assert doc.sections[0].heading_path == ["Record properties"]
    assert "TEST-42" in doc.sections[0].text


def test_stable_property_ids_survive_renames_and_preserve_date_range() -> None:
    page = fixture("task_page_response.json")
    page["properties"]["Workflow"] = page["properties"].pop("Status")
    page["properties"]["Reviewers"] = {
        "id": "reviewers", "type": "people", "people": [{"id": OTHER_ID, "name": "Test Reviewer"}],
    }
    page["properties"]["Due Date"]["date"] = {
        "start": "2026-11-04T09:00:00", "end": "2026-11-04T10:00:00",
        "time_zone": "America/Chicago",
    }
    policy = NotionSourcePolicy(
        document_kind="project", ancestor_path=("Synthetic Team", "Projects"),
        property_overrides={"status": "state", "attendees": "reviewers"},
    )
    doc, _ = normalize(page, policy=policy)
    assert doc.metadata["document_kind"] == "project"
    assert doc.metadata["ancestor_path"] == ["Synthetic Team", "Projects"]
    assert doc.metadata["ancestor_path_origin"] == "source_policy"
    assert doc.metadata["properties"]["attendees"][0]["name"] == "Test Reviewer"
    assert doc.metadata["properties"]["due_date"]["end"] == "2026-11-04T10:00:00"
    assert "America/Chicago" in doc.sections[-1].text


def test_ambiguous_property_aliases_require_explicit_mapping() -> None:
    page = fixture("task_page_response.json")
    page["properties"]["Assignee"] = deepcopy(page["properties"]["Assigned To"])
    page["properties"]["Assignee"]["id"] = "other_people"
    with pytest.raises(NotionResponseError, match="Ambiguous assignees"):
        normalize(page)
    doc, _ = normalize(
        page, policy=NotionSourcePolicy(property_overrides={"assignees": "owners"}),
    )
    assert len(doc.metadata["properties"]["assignees"]) == 1


def test_missing_configured_status_is_not_treated_as_statusless() -> None:
    with pytest.raises(NotionResponseError, match="Configured status property is missing"):
        normalize(
            fixture("task_page_response.json"),
            policy=NotionSourcePolicy(property_overrides={"status": "nonexistent"}),
        )


def test_navigation_hub_policy_excludes_a_page() -> None:
    with pytest.raises(NotionRecordExcludedError, match="navigation_hub"):
        normalize(
            fixture("task_page_response.json"), policy=NotionSourcePolicy(document_kind="hub"),
        )


@pytest.mark.parametrize(("title_field", "expected"), [
    ("Meeting name", "meeting_note"), ("Project name", "project"),
    ("Doc name", "technical_doc"), ("Unusual name", "technical_doc"),
])
def test_kind_uses_title_property_type_and_source_conventions(title_field, expected) -> None:
    page = fixture("task_page_response.json")
    page["properties"][title_field] = page["properties"].pop("Task Name")
    doc, _ = normalize(page)
    assert doc.metadata["document_kind"] == expected


def test_duplicate_titles_keep_distinct_source_identity() -> None:
    first = fixture("task_page_response.json")
    second = deepcopy(first)
    second["id"] = OTHER_ID
    second["url"] = f"https://www.notion.so/{OTHER_ID.replace('-', '')}"
    a, _ = normalize(first)
    b, _ = normalize(second)
    assert a.title == b.title
    assert a.external_id != b.external_id
    assert a.source_url != b.source_url


def test_batch_filters_rechecks_status_and_preserves_source_context() -> None:
    active = fixture("task_page_response.json")
    completed = deepcopy(active)
    completed["id"] = OTHER_ID
    completed["properties"]["Status"]["status"]["name"] = "Completed"
    paths: list[str] = []
    query_bodies: list[dict] = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == f"/v1/data_sources/{SOURCE_ID}":
            return httpx.Response(200, json=fixture("task_data_source_response.json"))
        if request.url.path.endswith("/query"):
            body = json.loads(request.content)
            query_bodies.append(body)
            if "start_cursor" not in body:
                return httpx.Response(200, json={
                    "results": [active], "has_more": True, "next_cursor": "next",
                })
            return httpx.Response(200, json={
                "results": [active, completed], "has_more": False,
            })
        if request.url.path.endswith("/markdown"):
            return httpx.Response(200, json=fixture("task_markdown_response.json"))
        return httpx.Response(200, json=completed if OTHER_ID in request.url.path else active)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = NotionConnector(
                token="synthetic", allowed_page_ids=[], allowed_data_source_ids=[SOURCE_ID],
                source_policies={SOURCE_ID: NotionSourcePolicy(ancestor_path=("Synthetic Team",))},
                client=client,
            )
            return await connector.fetch_and_normalize_data_source(SOURCE_ID)

    batch = asyncio.run(scenario())
    assert [doc.external_id for doc in batch.documents] == [PAGE_ID]
    assert batch.documents[0].metadata["data_source_name"] == "Synthetic task tracker"
    assert batch.documents[0].metadata["ancestor_path"] == ["Synthetic Team"]
    assert [(item.page_id, item.reason) for item in batch.skipped] == [
        (OTHER_ID, "unpublished_or_inactive_status"),
    ]
    assert f"/v1/pages/{OTHER_ID}/markdown" not in paths
    assert query_bodies[0]["filter"] == {"or": [
        {"property": "state", "status": {"equals": "Not Started"}},
        {"property": "state", "status": {"equals": "In Progress"}},
        {"property": "state", "status": {"equals": "Waiting"}},
    ]}
    assert query_bodies[0]["filter"] == query_bodies[1]["filter"]


@pytest.mark.parametrize("failure", ["moved", "missing_status", "missing_assignees"])
def test_discovered_record_must_still_belong_and_match_schema(failure: str) -> None:
    page = fixture("task_page_response.json")
    if failure == "moved":
        page["parent"]["data_source_id"] = OTHER_ID
    elif failure == "missing_status":
        del page["properties"]["Status"]
    else:
        del page["properties"]["Assigned To"]
    paths: list[str] = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == f"/v1/data_sources/{SOURCE_ID}":
            return httpx.Response(200, json=fixture("task_data_source_response.json"))
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": [page], "has_more": False})
        return httpx.Response(200, json=page)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = NotionConnector(
                token="synthetic", allowed_page_ids=[], allowed_data_source_ids=[SOURCE_ID],
                client=client,
            )
            expected = NotionSourceNotAllowedError if failure == "moved" else NotionResponseError
            with pytest.raises(expected):
                await connector.fetch_and_normalize_data_source(SOURCE_ID)

    asyncio.run(scenario())
    assert not any(path.endswith("/markdown") for path in paths)


@pytest.mark.parametrize("failure", ["incomplete", "repeated_cursor"])
def test_failed_discovery_does_not_grant_page_access(failure: str) -> None:
    def handler(request):
        assert request.url.path.endswith("/query")
        payload = {
            "results": [fixture("task_page_response.json")],
            "has_more": True, "next_cursor": "repeated",
        }
        if failure == "incomplete":
            payload["request_status"] = {"type": "incomplete"}
        return httpx.Response(200, json=payload)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = NotionConnector(
                token="synthetic", allowed_page_ids=[], allowed_data_source_ids=[SOURCE_ID],
                client=client,
            )
            expected = (
                NotionIncompleteQueryError if failure == "incomplete" else NotionResponseError
            )
            with pytest.raises(expected):
                await connector.list_notion_pages(SOURCE_ID)
            with pytest.raises(NotionSourceNotAllowedError):
                await connector.fetch_notion_page(PAGE_ID)

    asyncio.run(scenario())


def test_selected_people_property_is_paginated_and_sanitized() -> None:
    page = fixture("task_page_response.json")
    people = [{"id": f"synthetic-user-{i}", "name": f"Test Engineer {i}"} for i in range(26)]
    page["properties"]["Assigned To"]["people"] = people[:25]
    page["properties"]["Assigned To"]["id"] = "x%3Ay"
    property_requests: list[httpx.Request] = []

    def handler(request):
        if "/properties/" in request.url.path:
            property_requests.append(request)
            assert request.url.raw_path.split(b"?")[0].endswith(b"/x%3Ay")
            second = "start_cursor" in request.url.params
            values = people[25:] if second else people[:25]
            return httpx.Response(200, json={
                "object": "list",
                "results": [{"type": "people", "people": value} for value in values],
                "has_more": not second, "next_cursor": None if second else "next",
            })
        if request.url.path.endswith("/markdown"):
            return httpx.Response(200, json=fixture("task_markdown_response.json"))
        return httpx.Response(200, json=page)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = NotionConnector(
                token="synthetic", allowed_page_ids=[PAGE_ID], client=client,
            )
            return await connector.fetch_and_normalize_page(PAGE_ID)

    doc = asyncio.run(scenario())
    assert len(property_requests) == 2
    assert len(doc.metadata["properties"]["assignees"]) == 26
    assert "Test Engineer 25" in doc.sections[-1].text


def test_policies_do_not_grant_approval() -> None:
    with pytest.raises(ValueError, match="independently allowlisted"):
        NotionConnector(
            token="synthetic", allowed_page_ids=[],
            source_policies={PAGE_ID: NotionSourcePolicy()},
        )


def test_policy_settings_parse_json_and_reject_unknown_fields(monkeypatch) -> None:
    monkeypatch.setenv("NOTION_SOURCE_POLICIES", json.dumps({
        PAGE_ID: {"document_kind": "meeting_note"},
    }))
    settings = Settings(_env_file=None)
    assert settings.notion_source_policies[PAGE_ID].document_kind == "meeting_note"
    with pytest.raises(ValidationError):
        NotionSourcePolicy(property_overrides={"email": "Contact email"})
    with pytest.raises(ValidationError):
        NotionSourcePolicy(ancestor_path=("",))


@pytest.mark.parametrize("property_value", [None, "Published", {"status": {"name": "Published"}}])
def test_malformed_status_property_cannot_become_implicitly_active(property_value) -> None:
    page = fixture("task_page_response.json")
    page["properties"]["Status"] = property_value
    with pytest.raises(NotionResponseError, match="malformed property"):
        normalize(page)
