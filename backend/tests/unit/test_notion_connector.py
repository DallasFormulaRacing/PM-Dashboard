"""Contract tests for the allowlist-enforcing Notion connector."""

import asyncio
from collections.abc import Awaitable, Callable
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.connectors.notion import (
    NotionAPIError,
    NotionConnector,
    NotionIncompleteContentError,
    NotionIncompleteQueryError,
    NotionSourceNotAllowedError,
)
from app.domain.documents import NormalizedDocument


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "notion"
PAGE_ID = "11111111-1111-4111-8111-111111111111"
BLOCK_ID = "22222222-2222-4222-8222-222222222222"
DATA_SOURCE_ID = "33333333-3333-4333-8333-333333333333"
DISCOVERED_PAGE_ID = "44444444-4444-4444-8444-444444444444"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def run(coroutine: Awaitable[Any]) -> Any:
    return asyncio.run(coroutine)


async def no_sleep(_: float) -> None:
    return None


def make_connector(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    allowed_page_ids: tuple[str, ...] = (PAGE_ID,),
    allowed_data_source_ids: tuple[str, ...] = (),
    sleep: Callable[[float], Awaitable[None]] = no_sleep,
    max_retries: int = 3,
) -> tuple[NotionConnector, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = NotionConnector(
        token="synthetic-test-token",
        allowed_page_ids=allowed_page_ids,
        allowed_data_source_ids=allowed_data_source_ids,
        client=client,
        sleep=sleep,
        jitter=lambda _: 0,
        max_retries=max_retries,
    )
    return connector, client


def test_approved_page_normalizes_to_saved_fixture() -> None:
    page_response = load_fixture("page_response.json")
    markdown_response = load_fixture("markdown_response.json")
    expected = NormalizedDocument.model_validate(load_fixture("announcement.json"))
    observed_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_requests.append(request)
        if request.url.path.endswith("/markdown"):
            return httpx.Response(200, json=markdown_response)
        return httpx.Response(200, json=page_response)

    async def scenario() -> NormalizedDocument:
        connector, client = make_connector(handler)
        try:
            return await connector.fetch_and_normalize_page(PAGE_ID)
        finally:
            await client.aclose()

    actual = run(scenario())

    assert actual == expected
    assert "[applications](https://example.com/apply)" in actual.sections[0].text
    assert {request.headers["Notion-Version"] for request in observed_requests} == {"2026-03-11"}
    assert all(
        request.headers["Authorization"] == "Bearer synthetic-test-token"
        for request in observed_requests
    )


def test_unapproved_page_is_rejected_before_network_io() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async def scenario() -> None:
        connector, client = make_connector(handler)
        try:
            with pytest.raises(NotionSourceNotAllowedError):
                await connector.fetch_notion_page(BLOCK_ID)
        finally:
            await client.aclose()

    run(scenario())
    assert calls == 0


@pytest.mark.parametrize("status_code", [403, 404])
def test_inaccessible_page_returns_controlled_error(status_code: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "object": "error",
                "status": status_code,
                "code": "object_not_found" if status_code == 404 else "restricted_resource",
                "message": "Synthetic access failure",
            },
        )

    async def scenario() -> None:
        connector, client = make_connector(handler, max_retries=0)
        try:
            with pytest.raises(NotionAPIError) as captured:
                await connector.fetch_notion_page(PAGE_ID)
            assert captured.value.status_code == status_code
        finally:
            await client.aclose()

    run(scenario())


def test_truncated_markdown_recovers_nested_content() -> None:
    primary = load_fixture("markdown_response.json")
    primary.update(truncated=True, unknown_block_ids=[BLOCK_ID])
    primary["markdown"] += f'\n\n<unknown url="https://www.notion.so/{PAGE_ID}#{BLOCK_ID}" />'
    subtree = {
        "object": "page_markdown",
        "id": BLOCK_ID,
        "markdown": "### Nested Details\n\nRecovered nested content.",
        "truncated": False,
        "unknown_block_ids": [],
        "warnings": [{"code": "unsupported_embed", "message": "Embed preserved as a link"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if BLOCK_ID in request.url.path:
            return httpx.Response(200, json=subtree)
        return httpx.Response(200, json=primary)

    async def scenario():
        connector, client = make_connector(handler)
        try:
            return await connector.fetch_notion_markdown(PAGE_ID)
        finally:
            await client.aclose()

    result = run(scenario())

    assert result.complete is True
    assert result.recovered_block_ids == (BLOCK_ID,)
    assert result.unresolved_block_ids == ()
    assert "Recovered nested content." in result.markdown
    assert result.warnings[0].code == "unsupported_embed"


def test_unresolved_truncated_content_prevents_normalization() -> None:
    page_response = load_fixture("page_response.json")
    primary = load_fixture("markdown_response.json")
    primary.update(truncated=True, unknown_block_ids=[BLOCK_ID])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/pages/{PAGE_ID}":
            return httpx.Response(200, json=page_response)
        if BLOCK_ID in request.url.path:
            return httpx.Response(
                404,
                json={
                    "object": "error",
                    "status": 404,
                    "code": "object_not_found",
                    "message": "Synthetic inaccessible child",
                },
            )
        return httpx.Response(200, json=primary)

    async def scenario() -> None:
        connector, client = make_connector(handler)
        try:
            with pytest.raises(NotionIncompleteContentError) as captured:
                await connector.fetch_and_normalize_page(PAGE_ID)
            assert captured.value.unresolved_block_ids == (BLOCK_ID,)
        finally:
            await client.aclose()

    run(scenario())


def test_truncated_response_without_block_ids_is_not_silently_accepted() -> None:
    response = load_fixture("markdown_response.json")
    response.update(truncated=True, unknown_block_ids=[])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async def scenario():
        connector, client = make_connector(handler)
        try:
            return await connector.fetch_notion_markdown(PAGE_ID)
        finally:
            await client.aclose()

    result = run(scenario())
    assert result.was_truncated is True
    assert result.complete is False


def test_rate_limit_honors_retry_after_then_succeeds() -> None:
    attempts = 0
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                json={
                    "object": "error",
                    "status": 429,
                    "code": "rate_limited",
                    "message": "Synthetic rate limit",
                },
            )
        return httpx.Response(200, json=load_fixture("page_response.json"))

    async def scenario() -> None:
        connector, client = make_connector(handler, sleep=record_sleep)
        try:
            await connector.fetch_notion_page(PAGE_ID)
        finally:
            await client.aclose()

    run(scenario())
    assert attempts == 2
    assert delays == [2.0]


def test_blocked_rate_limit_is_not_retried() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "2"},
            json={
                "object": "error",
                "status": 429,
                "code": "public_api_request_blocked",
                "message": "Synthetic blocked connection",
            },
        )

    async def scenario() -> None:
        connector, client = make_connector(handler)
        try:
            with pytest.raises(NotionAPIError):
                await connector.fetch_notion_page(PAGE_ID)
        finally:
            await client.aclose()

    run(scenario())
    assert attempts == 1


def test_approved_data_source_paginates_and_authorizes_discovered_pages() -> None:
    query_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_count
        if request.url.path.endswith("/query"):
            query_count += 1
            request_body = json.loads(request.content)
            if "start_cursor" not in request_body:
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "results": [],
                        "has_more": True,
                        "next_cursor": "synthetic-cursor",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "results": [{"object": "page", "id": DISCOVERED_PAGE_ID}],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        page = load_fixture("page_response.json")
        page["id"] = DISCOVERED_PAGE_ID
        return httpx.Response(200, json=page)

    async def scenario() -> list[dict[str, Any]]:
        connector, client = make_connector(
            handler,
            allowed_page_ids=(),
            allowed_data_source_ids=(DATA_SOURCE_ID,),
        )
        try:
            pages = await connector.list_notion_announcements(DATA_SOURCE_ID)
            await connector.fetch_notion_page(DISCOVERED_PAGE_ID)
            return pages
        finally:
            await client.aclose()

    pages = run(scenario())
    assert query_count == 2
    assert [page["id"] for page in pages] == [DISCOVERED_PAGE_ID]


def test_unapproved_data_source_is_rejected() -> None:
    async def scenario() -> None:
        connector, client = make_connector(lambda _: httpx.Response(500))
        try:
            with pytest.raises(NotionSourceNotAllowedError):
                await connector.list_notion_announcements(DATA_SOURCE_ID)
        finally:
            await client.aclose()

    run(scenario())


def test_incomplete_data_source_query_is_reported() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": [],
                "has_more": False,
                "next_cursor": None,
                "request_status": {
                    "type": "incomplete",
                    "incomplete_reason": "query_result_limit_reached",
                },
            },
        )

    async def scenario() -> None:
        connector, client = make_connector(
            handler,
            allowed_data_source_ids=(DATA_SOURCE_ID,),
        )
        try:
            with pytest.raises(NotionIncompleteQueryError):
                await connector.list_notion_announcements(DATA_SOURCE_ID)
        finally:
            await client.aclose()

    run(scenario())


def test_recovered_subtree_stays_under_original_heading() -> None:
    primary = load_fixture("markdown_response.json")
    primary.update(
        markdown=(
            f'## Decisions\n\n<unknown url="https://www.notion.so/{PAGE_ID}#{BLOCK_ID}" />'
            "\n\n## Action items\n\nKeep this separate action."
        ),
        truncated=True,
        unknown_block_ids=[BLOCK_ID],
    )
    subtree = {
        "object": "page_markdown",
        "markdown": "Keep this recovered decision.",
        "truncated": False,
        "unknown_block_ids": [],
    }

    def handler(request: httpx.Request):
        if request.url.path == f"/v1/pages/{PAGE_ID}":
            return httpx.Response(200, json=load_fixture("page_response.json"))
        return httpx.Response(200, json=subtree if BLOCK_ID in request.url.path else primary)

    async def scenario():
        connector, client = make_connector(handler)
        try:
            return await connector.fetch_and_normalize_page(PAGE_ID)
        finally:
            await client.aclose()

    doc = run(scenario())
    assert doc.sections[0].text == "Keep this recovered decision."
    assert doc.sections[0].heading_path == ["Decisions"]
    assert doc.sections[1].heading_path == ["Action items"]


@pytest.mark.parametrize("cycle", [False, True])
def test_recovery_requires_a_location_and_rejects_cycles(cycle) -> None:
    primary = load_fixture("markdown_response.json")
    primary.update(truncated=True, unknown_block_ids=[BLOCK_ID])
    if cycle:
        primary["markdown"] += f'\n\n<unknown url="https://www.notion.so/{PAGE_ID}#{BLOCK_ID}" />'
    subtree = {
        "object": "page_markdown",
        "markdown": "Recovered text.",
        "truncated": False,
        "unknown_block_ids": [BLOCK_ID] if cycle else [],
    }

    def handler(request):
        return httpx.Response(200, json=subtree if BLOCK_ID in request.url.path else primary)

    async def scenario():
        connector, client = make_connector(handler)
        try:
            return await connector.fetch_notion_markdown(PAGE_ID)
        finally:
            await client.aclose()

    result = run(scenario())
    assert result.complete is False
    assert BLOCK_ID in result.unresolved_block_ids
