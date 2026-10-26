"""Allowlist-enforcing asynchronous connector for the Notion REST API."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import random
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

import httpx

from app.config import Settings, get_settings
from app.domain.documents import DocumentSection, NormalizedDocument
from app.ingestion.notion_markdown import MarkdownExtractionError, clean_notion_markdown
from app.connectors.notion_properties import (
    ACTIVE_STATUSES,
    PROPERTY_ALIASES,
    NotionPropertyError,
    NotionSourcePolicy,
    find_property,
    infer_document_kind,
    lifecycle_reason,
    normalize_properties,
    normalized_status,
    property_summary,
)


NOTION_API_BASE_URL = "https://api.notion.com"
DEFAULT_NOTION_API_VERSION = "2026-03-11"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})
BLOCKED_RATE_LIMIT_CODE = "public_api_request_blocked"


class NotionConnectorError(RuntimeError):
    """Base class for controlled Notion connector failures."""


class NotionSourceNotAllowedError(NotionConnectorError):
    """Raised before I/O when a source is outside the configured allowlist."""


class NotionAPIError(NotionConnectorError):
    """A sanitized non-success response from the Notion API."""

    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.api_message = message
        super().__init__(f"Notion API error {status_code} ({code}): {message}")


class NotionTransportError(NotionConnectorError):
    """Raised when bounded retries cannot reach the Notion API."""


class NotionResponseError(NotionConnectorError):
    """Raised when Notion returns an unexpected or malformed response."""


class NotionIncompleteContentError(NotionConnectorError):
    """Raised when an approved page cannot be normalized without silent gaps."""

    def __init__(self, unresolved_block_ids: Iterable[str]) -> None:
        self.unresolved_block_ids = tuple(unresolved_block_ids)
        detail = ", ".join(self.unresolved_block_ids) or "unknown blocks not identified"
        super().__init__(f"Notion page content is incomplete: {detail}")


class NotionIncompleteQueryError(NotionConnectorError):
    """Raised when a data-source query reaches Notion's result ceiling."""


class NotionRecordExcludedError(NotionConnectorError):
    """An approved record is ineligible for the current ingestion policy."""

    def __init__(self, page_id: str, reason: str) -> None:
        self.page_id = page_id
        self.reason = reason
        super().__init__(f"Notion record excluded: {page_id} ({reason})")


@dataclass(frozen=True, slots=True)
class NotionSkippedRecord:
    page_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class NotionBatchResult:
    """A complete in-memory batch; transport/extraction failures raise instead."""

    documents: tuple[NormalizedDocument, ...]
    skipped: tuple[NotionSkippedRecord, ...]


@dataclass(frozen=True, slots=True)
class NotionWarning:
    """Warning returned with a Markdown representation."""

    code: str
    message: str

    def as_metadata(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class NotionMarkdownResult:
    """Markdown plus explicit recovery and completeness information."""

    markdown: str
    was_truncated: bool
    unknown_block_ids: tuple[str, ...]
    recovered_block_ids: tuple[str, ...]
    unresolved_block_ids: tuple[str, ...]
    warnings: tuple[NotionWarning, ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class _MarkdownPayload:
    markdown: str
    truncated: bool
    unknown_block_ids: tuple[str, ...]
    warnings: tuple[NotionWarning, ...]


SleepCallable = Callable[[float], Awaitable[None]]
JitterCallable = Callable[[float], float]


class NotionConnector:
    """Read approved Notion sources and emit provider-independent documents."""

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        **connector_options: Any,
    ) -> NotionConnector:
        """Build a connector from validated application settings."""

        resolved_settings = settings or get_settings()
        if resolved_settings.notion_api_token is None:
            raise ValueError("NOTION_API_TOKEN is required")
        return cls(
            token=resolved_settings.notion_api_token.get_secret_value(),
            allowed_page_ids=resolved_settings.notion_allowed_page_ids,
            allowed_data_source_ids=resolved_settings.notion_allowed_data_source_ids,
            source_policies=resolved_settings.notion_source_policies,
            api_version=resolved_settings.notion_api_version,
            **connector_options,
        )

    def __init__(
        self,
        *,
        token: str,
        allowed_page_ids: Iterable[str],
        allowed_data_source_ids: Iterable[str] = (),
        source_policies: Mapping[str, NotionSourcePolicy | Mapping[str, Any]] | None = None,
        api_version: str = DEFAULT_NOTION_API_VERSION,
        base_url: str = NOTION_API_BASE_URL,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        max_retry_delay_seconds: float = 60.0,
        max_unknown_blocks: int = 100,
        client: httpx.AsyncClient | None = None,
        sleep: SleepCallable = asyncio.sleep,
        jitter: JitterCallable | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("a Notion API token is required")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if max_retry_delay_seconds <= 0:
            raise ValueError("max_retry_delay_seconds must be positive")
        if max_unknown_blocks <= 0:
            raise ValueError("max_unknown_blocks must be positive")

        self._token = token
        self._allowed_page_ids = {_canonicalize_notion_id(value) for value in allowed_page_ids}
        self._allowed_data_source_ids = {
            _canonicalize_notion_id(value) for value in allowed_data_source_ids
        }
        self._discovered_page_ids: set[str] = set()
        self._page_sources: dict[str, str] = {}
        self._data_sources: dict[str, dict[str, Any]] = {}
        self._source_policies = {
            _canonicalize_notion_id(key): NotionSourcePolicy.model_validate(value)
            for key, value in (source_policies or {}).items()
        }
        if self._source_policies.keys() - (
            self._allowed_page_ids | self._allowed_data_source_ids
        ):
            raise ValueError("Source policies require independently allowlisted IDs")
        self._api_version = api_version
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._max_retry_delay_seconds = max_retry_delay_seconds
        self._max_unknown_blocks = max_unknown_blocks
        self._sleep = sleep
        self._jitter = jitter or (lambda delay: random.uniform(0, delay * 0.25))
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def __aenter__(self) -> NotionConnector:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally owned HTTP client."""

        if self._owns_client:
            await self._client.aclose()

    async def fetch_notion_page(self, page_id: str) -> dict[str, Any]:
        """Retrieve metadata for one explicitly or transitively approved page."""

        canonical_id = self._require_allowed_page(page_id)
        payload = await self._request("GET", f"/v1/pages/{quote(canonical_id, safe='')}")
        if payload.get("object") != "page":
            raise NotionResponseError("Notion page endpoint did not return a page object")
        return payload

    async def fetch_notion_markdown(self, page_id: str) -> NotionMarkdownResult:
        """Retrieve Markdown and attempt bounded recovery of unknown block subtrees."""

        canonical_id = self._require_allowed_page(page_id)
        primary = await self._fetch_markdown_resource(canonical_id)
        markdown = primary.markdown
        pending = list(primary.unknown_block_ids)
        original_unknown_ids = tuple(primary.unknown_block_ids)
        recovered: list[str] = []
        unresolved: list[str] = []
        warnings = list(primary.warnings)
        seen: set[str] = set()
        truncated_without_ids = primary.truncated and not pending

        while pending and len(seen) < self._max_unknown_blocks:
            block_id = pending.pop(0)
            if block_id in seen:
                continue
            seen.add(block_id)

            try:
                subtree = await self._fetch_markdown_resource(block_id)
            except NotionAPIError as error:
                if error.status_code == 404:
                    unresolved.append(block_id)
                    continue
                raise

            replaced = False

            def replace_unknown(match: re.Match[str]) -> str:
                nonlocal replaced
                attribute = re.search(r'''\burl=(["'])(.*?)\1''', match[0])
                if attribute:
                    url = urlsplit(attribute[2])
                    candidate = url.fragment or url.path.rstrip("/").rsplit("/", 1)[-1]
                    if candidate.replace("-", "") == block_id.replace("-", ""):
                        replaced = True
                        return subtree.markdown
                return match[0]

            markdown = re.sub(r"<unknown\b[^>]*/>", replace_unknown, markdown)
            if not replaced:
                unresolved.append(block_id)
                continue
            recovered.append(block_id)
            warnings.extend(subtree.warnings)
            if subtree.truncated and not subtree.unknown_block_ids:
                truncated_without_ids = True
            for child_id in subtree.unknown_block_ids:
                if child_id in seen:
                    unresolved.append(child_id)
                elif child_id not in pending:
                    pending.append(child_id)

        unresolved.extend(block_id for block_id in pending if block_id not in unresolved)
        complete = not unresolved and not truncated_without_ids

        return NotionMarkdownResult(
            markdown=markdown,
            was_truncated=primary.truncated,
            unknown_block_ids=original_unknown_ids,
            recovered_block_ids=tuple(recovered),
            unresolved_block_ids=tuple(unresolved),
            warnings=tuple(warnings),
            complete=complete,
        )

    async def list_notion_announcements(self, data_source_id: str) -> list[dict[str, Any]]:
        """Compatibility alias; new callers should use list_notion_pages."""

        return await self.list_notion_pages(data_source_id)

    async def fetch_notion_data_source(self, data_source_id: str) -> dict[str, Any]:
        canonical_id = self._require_allowed_data_source(data_source_id)
        payload = await self._request("GET", f"/v1/data_sources/{canonical_id}")
        if (
            payload.get("object") != "data_source"
            or payload.get("id") != canonical_id
            or not isinstance(payload.get("properties"), dict)
        ):
            raise NotionResponseError("Notion returned an invalid data-source schema")
        self._data_sources[canonical_id] = payload
        return payload

    async def list_notion_pages(
        self, data_source_id: str, *, query_filter: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Discover only direct rows; grant page access after a complete query.

        This low-level method returns raw metadata including inactive records.
        Use fetch_and_normalize_data_source for lifecycle-filtered documents.
        """

        canonical_id = self._require_allowed_data_source(data_source_id)
        previous = {key for key, value in self._page_sources.items() if value == canonical_id}
        self._discovered_page_ids.difference_update(previous)
        for key in previous:
            del self._page_sources[key]

        results: list[dict[str, Any]] = []
        discovered_ids: set[str] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while True:
            request_body: dict[str, Any] = {"page_size": 100}
            if query_filter is not None:
                request_body["filter"] = dict(query_filter)
            if cursor is not None:
                request_body["start_cursor"] = cursor

            payload = await self._request(
                "POST",
                f"/v1/data_sources/{quote(canonical_id, safe='')}/query",
                json=request_body,
            )
            raw_results = payload.get("results")
            if not isinstance(raw_results, list):
                raise NotionResponseError("Notion data-source query omitted results")

            for result in raw_results:
                if not isinstance(result, dict) or result.get("object") != "page":
                    continue
                page_id = result.get("id")
                if not isinstance(page_id, str):
                    raise NotionResponseError("Notion data-source page omitted its ID")
                canonical_page_id = _canonicalize_notion_id(page_id)
                if canonical_page_id not in discovered_ids:
                    discovered_ids.add(canonical_page_id)
                    results.append(result)

            request_status = payload.get("request_status")
            if isinstance(request_status, Mapping) and request_status.get("type") == "incomplete":
                reason = str(request_status.get("incomplete_reason", "unknown"))
                raise NotionIncompleteQueryError(
                    f"Notion data-source query was incomplete: {reason}"
                )

            has_more = payload.get("has_more")
            next_cursor = payload.get("next_cursor")
            if not isinstance(has_more, bool):
                raise NotionResponseError("Notion query omitted pagination status")
            if not has_more:
                self._discovered_page_ids.update(discovered_ids)
                self._page_sources.update({page_id: canonical_id for page_id in discovered_ids})
                return results
            if not isinstance(next_cursor, str) or not next_cursor:
                raise NotionResponseError(
                    "Notion data-source query reported more results without a cursor"
                )
            if next_cursor in seen_cursors:
                raise NotionResponseError("Notion query repeated a pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def fetch_and_normalize_data_source(self, data_source_id: str) -> NotionBatchResult:
        """Normalize eligible rows of one approved data source, without writes."""

        canonical_id = self._require_allowed_data_source(data_source_id)
        schema = await self.fetch_notion_data_source(canonical_id)
        policy = self._source_policies.get(canonical_id, NotionSourcePolicy())
        try:
            status = find_property(schema["properties"], "status", policy)
        except NotionPropertyError as error:
            raise NotionResponseError(str(error)) from error
        query_filter = None
        if status is not None and status[1].get("type") in {"status", "select"}:
            kind = status[1]["type"]
            settings = status[1].get(kind)
            if not isinstance(settings, Mapping) or not isinstance(settings.get("options"), list):
                raise NotionResponseError("Notion status schema omitted its options")
            options = settings["options"]
            conditions = [
                {"property": status[1].get("id", status[0]), kind: {"equals": option["name"]}}
                for option in options
                if isinstance(option, Mapping) and isinstance(option.get("name"), str)
                and normalized_status(option["name"]) in ACTIVE_STATUSES
            ]
            if conditions:
                query_filter = conditions[0] if len(conditions) == 1 else {"or": conditions}

        pages = await self.list_notion_pages(canonical_id, query_filter=query_filter)
        documents: list[NormalizedDocument] = []
        skipped: list[NotionSkippedRecord] = []
        for page in pages:
            try:
                # Re-fetch metadata and recheck status: query-time filters are
                # an optimization, not the enforcement boundary.
                documents.append(await self.fetch_and_normalize_page(page["id"]))
            except NotionRecordExcludedError as error:
                skipped.append(NotionSkippedRecord(error.page_id, error.reason))
        return NotionBatchResult(tuple(documents), tuple(skipped))

    async def fetch_and_normalize_page(self, page_id: str) -> NormalizedDocument:
        """Fetch one approved page and convert it to the common document contract."""

        page = await self.fetch_notion_page(page_id)
        canonical_id = self._require_allowed_page(page_id)
        if (
            not isinstance(page.get("id"), str)
            or _canonicalize_notion_id(page["id"]) != canonical_id
        ):
            raise NotionResponseError("Notion returned a different page than requested")
        parent = page.get("parent", {})
        if not isinstance(parent, Mapping):
            raise NotionResponseError("Notion page has an invalid parent")
        parent_type = parent.get("type")
        parent_id = parent.get(parent_type) if isinstance(parent_type, str) else None
        data_source_id = parent.get("data_source_id")
        if data_source_id is not None:
            if not isinstance(data_source_id, str):
                raise NotionResponseError("Notion page has an invalid data-source parent")
            data_source_id = _canonicalize_notion_id(data_source_id)
        if parent_id is not None and not isinstance(parent_id, (str, bool)):
            raise NotionResponseError("Notion page has an invalid parent ID")
        discovered_source = self._page_sources.get(canonical_id)
        if discovered_source and data_source_id != discovered_source:
            self._discovered_page_ids.discard(canonical_id)
            self._page_sources.pop(canonical_id, None)
            raise NotionSourceNotAllowedError(
                "Discovered page moved outside its approved data source"
            )
        policy = self._source_policies.get(
            canonical_id, self._source_policies.get(data_source_id, NotionSourcePolicy()),
        )
        try:
            self._validate_property_schema(page, data_source_id, policy)
            reason = lifecycle_reason(page, policy)
            if reason:
                raise NotionRecordExcludedError(canonical_id, reason)
            if policy.document_kind == "hub":
                raise NotionRecordExcludedError(canonical_id, "navigation_hub")
            page = await self._complete_selected_properties(page, policy)
            normalized_properties = normalize_properties(page, policy)
            document_kind = infer_document_kind(page, policy)
        except NotionPropertyError as error:
            raise NotionResponseError(str(error)) from error

        markdown_result = await self.fetch_notion_markdown(page_id)
        if not markdown_result.complete:
            raise NotionIncompleteContentError(markdown_result.unresolved_block_ids)

        try:
            cleanup = clean_notion_markdown(
                markdown_result.markdown,
                remove_template_placeholders=policy.remove_template_placeholders,
            )
        except MarkdownExtractionError as error:
            raise NotionResponseError(str(error)) from error
        sections = list(cleanup.sections)
        summary = property_summary(normalized_properties)
        if summary:
            sections.append(DocumentSection(
                text=summary, heading_path=["Record properties"], kind="properties",
                section_reference="notion:properties",
            ))
        if not sections:
            raise NotionResponseError("Notion page contains no extractable text")

        title = _extract_title(page)
        source_url = page.get("url")
        if not isinstance(source_url, str) or not source_url:
            raise NotionResponseError("Notion page response omitted its canonical URL")

        last_edited_time = _required_datetime(page, "last_edited_time")
        created_time = _optional_datetime(page.get("created_time"))
        metadata: dict[str, Any] = {
            "document_kind": document_kind,
            "parent_type": parent_type,
            "parent_id": parent_id,
            "data_source_id": data_source_id,
            "data_source_name": _plain_text(
                self._data_sources.get(data_source_id, {}).get("title")
            ),
            "ancestor_path": list(policy.ancestor_path),
            "ancestor_path_origin": "source_policy" if policy.ancestor_path else "unavailable",
            "property_schema_version": "notion-v1",
            "lifecycle_policy": (
                "published_or_active" if find_property(page["properties"], "status", policy)
                else "implicit_active"
            ),
            "properties": normalized_properties,
            "notion_cleanup": cleanup.metadata(),
            "notion_created_time": created_time.isoformat() if created_time else None,
            "notion_archived": bool(page.get("archived", False)),
            "notion_in_trash": bool(page.get("in_trash", False)),
            "notion_markdown": {
                "version": "v1",
                "was_truncated": markdown_result.was_truncated,
                "unknown_block_ids": list(markdown_result.unknown_block_ids),
                "recovered_block_ids": list(markdown_result.recovered_block_ids),
                "unresolved_block_ids": list(markdown_result.unresolved_block_ids),
                "warnings": [warning.as_metadata() for warning in markdown_result.warnings],
            },
        }
        categories = normalized_properties.get("categories")
        if categories:
            metadata["category"] = categories[0] if len(categories) == 1 else categories

        return NormalizedDocument(
            source="notion",
            external_id=canonical_id,
            title=title,
            source_url=source_url,
            source_version=last_edited_time.isoformat(),
            modified_at=last_edited_time,
            sections=sections,
            metadata=metadata,
        )

    def _require_allowed_data_source(self, data_source_id: str) -> str:
        canonical_id = _canonicalize_notion_id(data_source_id)
        if canonical_id not in self._allowed_data_source_ids:
            raise NotionSourceNotAllowedError(f"Notion data source is not approved: {canonical_id}")
        return canonical_id

    def _validate_property_schema(
        self, page: Mapping[str, Any], data_source_id: str | None, policy: NotionSourcePolicy,
    ) -> None:
        """Missing declared properties cannot turn a row into a statusless page."""

        schema = self._data_sources.get(data_source_id)
        if schema is None:
            return
        properties = page.get("properties")
        if not isinstance(properties, Mapping):
            raise NotionPropertyError("Notion page omitted properties")
        for field in PROPERTY_ALIASES:
            expected = find_property(schema["properties"], field, policy)
            actual = find_property(properties, field, policy)
            if expected is not None and (
                actual is None or actual[1].get("type") != expected[1].get("type")
                or actual[1].get("id") != expected[1].get("id")
            ):
                raise NotionPropertyError(f"Notion row is missing its declared {field} property")

    async def _complete_selected_properties(
        self, page: dict[str, Any], policy: NotionSourcePolicy,
    ) -> dict[str, Any]:
        """Read possibly capped selected people/text properties via their endpoint."""

        properties = dict(page["properties"])
        names = {
            match[0] for field in PROPERTY_ALIASES
            if (match := find_property(properties, field, policy)) is not None
        }
        names.update(
            name for name, prop in properties.items()
            if isinstance(prop, Mapping) and prop.get("type") == "title"
        )
        for name in sorted(names):
            prop = properties[name]
            kind = prop.get("type")
            if kind not in {"people", "rich_text", "title"}:
                continue
            items = prop.get(kind)
            if not isinstance(items, list):
                raise NotionPropertyError("Notion selected property is not an array")
            if len(items) < 25 and not prop.get("has_more"):
                continue
            property_id = prop.get("id")
            if not isinstance(property_id, str):
                raise NotionPropertyError("Notion paginated property omitted its ID")
            # Property IDs in responses are already URL encoded.
            path = f"/v1/pages/{page['id']}/properties/{quote(unquote(property_id), safe='')}"
            completed: list[Any] = []
            cursor: str | None = None
            seen: set[str] = set()
            while True:
                params = {"page_size": "100"}
                if cursor:
                    params["start_cursor"] = cursor
                payload = await self._request("GET", path, params=params)
                rows = payload.get("results")
                if payload.get("object") != "list" or not isinstance(rows, list):
                    raise NotionPropertyError("Notion property pagination returned an invalid list")
                for row in rows:
                    if not isinstance(row, dict) or row.get("type") != kind or kind not in row:
                        raise NotionPropertyError(
                            "Notion property pagination returned an invalid item"
                        )
                    completed.append(row[kind])
                if not isinstance(payload.get("has_more"), bool):
                    raise NotionPropertyError("Notion property pagination omitted completeness")
                if not payload["has_more"]:
                    break
                cursor = payload.get("next_cursor")
                if (
                    not isinstance(cursor, str) or not cursor
                    or cursor in seen or len(completed) > 10000
                ):
                    raise NotionPropertyError("Notion property pagination did not complete")
                seen.add(cursor)
            properties[name] = {**prop, kind: completed, "has_more": False}
        return {**page, "properties": properties}

    async def _fetch_markdown_resource(self, resource_id: str) -> _MarkdownPayload:
        payload = await self._request(
            "GET",
            f"/v1/pages/{quote(resource_id, safe='')}/markdown",
            params={"markdown_version": "v1", "include_transcript": "false"},
        )
        return _parse_markdown_payload(payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._api_version,
            "Accept": "application/json",
        }
        if json is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=headers,
                    json=json,
                    params=params,
                    timeout=self._timeout_seconds,
                )
            except httpx.TransportError as error:
                if attempt >= self._max_retries:
                    raise NotionTransportError(
                        "Notion request failed after bounded transport retries"
                    ) from error
                await self._sleep(self._backoff_delay(attempt))
                continue

            payload = _response_json(response)
            if response.is_success:
                return payload

            code = str(payload.get("code", "unknown_error"))
            message = str(payload.get("message", "Notion request failed"))
            retryable = (
                response.status_code in RETRYABLE_STATUS_CODES
                and code != BLOCKED_RATE_LIMIT_CODE
            )
            if retryable and attempt < self._max_retries:
                await self._sleep(self._retry_delay(response, attempt))
                continue
            raise NotionAPIError(
                status_code=response.status_code,
                code=code,
                message=message,
            )

        raise AssertionError("retry loop exited unexpectedly")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(float(retry_after), self._max_retry_delay_seconds)
            except ValueError:
                pass
        return self._backoff_delay(attempt)

    def _backoff_delay(self, attempt: int) -> float:
        base_delay = min(2**attempt, self._max_retry_delay_seconds)
        return min(base_delay + self._jitter(base_delay), self._max_retry_delay_seconds)

    def _require_allowed_page(self, page_id: str) -> str:
        canonical_id = _canonicalize_notion_id(page_id)
        if (
            canonical_id not in self._allowed_page_ids
            and canonical_id not in self._discovered_page_ids
        ):
            raise NotionSourceNotAllowedError(f"Notion page is not approved: {canonical_id}")
        return canonical_id


def _canonicalize_notion_id(value: str) -> str:
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as error:
        raise ValueError(f"invalid Notion UUID: {value!r}") from error


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise NotionResponseError("Notion returned a non-JSON response") from error
    if not isinstance(payload, dict):
        raise NotionResponseError("Notion returned a non-object JSON response")
    return payload


def _parse_markdown_payload(payload: Mapping[str, Any]) -> _MarkdownPayload:
    if payload.get("object") != "page_markdown":
        raise NotionResponseError("Notion Markdown endpoint returned an unexpected object")
    markdown = payload.get("markdown")
    truncated = payload.get("truncated")
    unknown_block_ids = payload.get("unknown_block_ids")
    raw_warnings = payload.get("warnings", [])
    if not isinstance(markdown, str) or not isinstance(truncated, bool):
        raise NotionResponseError("Notion Markdown response has invalid content fields")
    if not isinstance(unknown_block_ids, list) or not all(
        isinstance(block_id, str) for block_id in unknown_block_ids
    ):
        raise NotionResponseError("Notion Markdown response has invalid unknown block IDs")
    if not isinstance(raw_warnings, list):
        raise NotionResponseError("Notion Markdown response has invalid warnings")

    warnings: list[NotionWarning] = []
    for warning in raw_warnings:
        if not isinstance(warning, Mapping):
            raise NotionResponseError("Notion Markdown warning has an invalid shape")
        warnings.append(
            NotionWarning(
                code=str(warning.get("code", "unknown_warning")),
                message=str(warning.get("message", "Notion reported a content warning")),
            )
        )

    canonical_unknown_ids: list[str] = []
    for block_id in unknown_block_ids:
        try:
            canonical_unknown_ids.append(_canonicalize_notion_id(block_id))
        except ValueError as error:
            raise NotionResponseError("Notion returned an invalid unknown block ID") from error

    return _MarkdownPayload(
        markdown=markdown,
        truncated=truncated,
        unknown_block_ids=tuple(canonical_unknown_ids),
        warnings=tuple(warnings),
    )


def _extract_title(page: Mapping[str, Any]) -> str:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        raise NotionResponseError("Notion page response omitted properties")

    for property_value in properties.values():
        if not isinstance(property_value, Mapping) or property_value.get("type") != "title":
            continue
        title = _plain_text(property_value.get("title"))
        if title:
            return title
    raise NotionResponseError("Notion page has no readable title property")


def _plain_text(rich_text: object) -> str:
    if not isinstance(rich_text, list):
        return ""
    parts: list[str] = []
    for item in rich_text:
        if not isinstance(item, Mapping):
            continue
        plain_text = item.get("plain_text")
        if isinstance(plain_text, str):
            parts.append(plain_text)
            continue
        text_value = item.get("text")
        if isinstance(text_value, Mapping) and isinstance(text_value.get("content"), str):
            parts.append(text_value["content"])
    return "".join(parts).strip()


def _required_datetime(page: Mapping[str, Any], field: str) -> datetime:
    value = _optional_datetime(page.get(field))
    if value is None:
        raise NotionResponseError(f"Notion page response omitted {field}")
    return value


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NotionResponseError("Notion page returned a malformed timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise NotionResponseError("Notion page returned a malformed timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NotionResponseError("Notion page returned a timestamp without a timezone")
    return parsed
