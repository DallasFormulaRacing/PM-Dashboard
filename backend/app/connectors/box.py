"""Allowlist-enforcing, read-only asynchronous connector for the Box API.

Only explicitly approved file IDs are read. Downloads stream to a private temporary
file with a size cap, are verified against Box's SHA-1, and are always deleted.
Temporary signed download URLs are never stored or used as citations.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import mimetypes
import os
from pathlib import Path
import random
import re
import tempfile
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.config import Settings, get_settings
from app.connectors.box_auth import (
    BoxAuth,
    BoxAuthError,
    DeveloperTokenAuth,
    OAuthTokenStore,
    OAuthUserAuth,
)
from app.domain.documents import NormalizedDocument
from app.ingestion.box_extract import (
    SUPPORTED_EXTENSIONS,
    BoxExtractionError,
    ExtractionResult,
    extract_file,
)


BOX_API_BASE_URL = "https://api.box.com/2.0"
BOX_WEB_FILE_URL = "https://app.box.com/file/{file_id}"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
FILE_FIELDS = (
    "id,type,name,size,sha1,etag,file_version,modified_at,"
    "trashed_at,item_status,parent,extension"
)
DOWNLOAD_CHUNK_BYTES = 64 * 1024

SleepCallable = Callable[[float], Awaitable[None]]


class BoxConnectorError(RuntimeError):
    """Base class for Box connector failures."""


class BoxSourceNotAllowedError(BoxConnectorError):
    """The file ID is not on the configured allowlist; no request was made."""


class BoxAPIError(BoxConnectorError):
    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(f"Box API error {status_code} ({code}): {message}")
        self.status_code = status_code
        self.code = code


class BoxAccessLostError(BoxAPIError):
    """403: the authenticated identity can no longer read this file."""


class BoxNotFoundError(BoxAPIError):
    """404: deleted, or not shared with the authenticated identity."""


class BoxTransportError(BoxConnectorError):
    pass


class BoxResponseError(BoxConnectorError):
    """Box returned an unexpected or malformed response."""


class BoxFileExcludedError(BoxConnectorError):
    """Policy exclusion decided from metadata, before any content download."""

    def __init__(self, file_id: str, reason: str) -> None:
        super().__init__(f"Box file {file_id} excluded: {reason}")
        self.file_id = file_id
        self.reason = reason


class BoxIncompleteContentError(BoxConnectorError):
    """Downloaded content could not be extracted completely and trustworthily."""


@dataclass(frozen=True)
class BoxFileMetadata:
    id: str
    name: str
    extension: str
    size: int
    sha1: str | None
    etag: str | None
    version_id: str | None
    modified_at: datetime | None
    trashed_at: datetime | None
    item_status: str | None
    parent_id: str | None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> BoxFileMetadata:
        if payload.get("type") != "file" or not isinstance(payload.get("id"), str):
            raise BoxResponseError("Box returned a non-file object for a file request")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BoxResponseError("Box file has no name")
        extension = payload.get("extension")
        if not isinstance(extension, str) or not extension:
            extension = Path(name).suffix.lstrip(".")
        size = payload.get("size")
        if not isinstance(size, int) or size < 0:
            raise BoxResponseError("Box file has no valid size")
        version = payload.get("file_version")
        parent = payload.get("parent")
        return cls(
            id=payload["id"],
            name=name.strip(),
            extension=extension.lower(),
            size=size,
            sha1=_optional_str(payload.get("sha1")),
            etag=_optional_str(payload.get("etag")),
            version_id=_optional_str(version.get("id")) if isinstance(version, dict) else None,
            modified_at=_optional_datetime(payload.get("modified_at")),
            trashed_at=_optional_datetime(payload.get("trashed_at")),
            item_status=_optional_str(payload.get("item_status")),
            parent_id=_optional_str(parent.get("id")) if isinstance(parent, dict) else None,
        )


class BoxConnector:
    """Read approved Box files and emit provider-independent documents."""

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        **connector_options: Any,
    ) -> BoxConnector:
        s = settings or get_settings()
        if not s.box_account_label or not s.box_account_label.strip():
            raise ValueError("BOX_ACCOUNT_LABEL is required (identifies the source account)")
        client = connector_options.pop("client", None) or httpx.AsyncClient()
        if s.box_auth_mode == "developer_token":
            if s.box_developer_token is None:
                raise ValueError("BOX_DEVELOPER_TOKEN is required for developer_token mode")
            auth: BoxAuth = DeveloperTokenAuth(s.box_developer_token.get_secret_value())
        elif s.box_auth_mode == "oauth":
            if not s.box_client_id or s.box_client_secret is None:
                raise ValueError("BOX_CLIENT_ID and BOX_CLIENT_SECRET are required for oauth")
            auth = OAuthUserAuth(
                client_id=s.box_client_id,
                client_secret=s.box_client_secret.get_secret_value(),
                store=OAuthTokenStore(s.box_oauth_token_path),
                client=client,
            )
        else:
            raise ValueError("BOX_AUTH_MODE must be 'developer_token' or 'oauth'")
        return cls(
            auth=auth,
            account_label=s.box_account_label,
            allowed_file_ids=s.box_allowed_file_ids,
            allowed_extensions=s.box_allowed_extensions,
            max_file_bytes=s.box_max_file_bytes,
            timeout_seconds=s.box_request_timeout_seconds,
            client=client,
            owns_client=True,
            **connector_options,
        )

    def __init__(
        self,
        *,
        auth: BoxAuth,
        account_label: str,
        allowed_file_ids: Iterable[str],
        allowed_extensions: Iterable[str] = SUPPORTED_EXTENSIONS,
        max_file_bytes: int = 25 * 1024 * 1024,
        base_url: str = BOX_API_BASE_URL,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        max_retry_delay_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
        owns_client: bool | None = None,
        sleep: SleepCallable = asyncio.sleep,
        jitter: Callable[[float], float] | None = None,
        temp_dir: Path | None = None,
        extractor: Callable[[Path, str], ExtractionResult] = extract_file,
    ) -> None:
        if not account_label.strip():
            raise ValueError("account_label is required")
        if max_file_bytes <= 0 or max_retries < 0:
            raise ValueError("invalid limits")
        unsupported = {e.lower().lstrip(".") for e in allowed_extensions} - SUPPORTED_EXTENSIONS
        if unsupported:
            raise ValueError(f"No extractor for allowed extensions: {sorted(unsupported)}")
        self._auth = auth
        self._account_label = account_label.strip()
        self._allowed_file_ids = {_canonicalize_box_id(value) for value in allowed_file_ids}
        self._allowed_extensions = {e.lower().lstrip(".") for e in allowed_extensions}
        self._max_file_bytes = max_file_bytes
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._max_retry_delay = max_retry_delay_seconds
        self._sleep = sleep
        self._jitter = jitter or (lambda delay: random.uniform(0, delay * 0.25))
        self._temp_dir = temp_dir
        self._extractor = extractor
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None if owns_client is None else owns_client

    async def __aenter__(self) -> BoxConnector:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def allowed_file_ids(self) -> list[str]:
        return sorted(self._allowed_file_ids)

    # ------------------------------------------------------------ public API

    async def fetch_box_metadata(self, file_id: str) -> BoxFileMetadata:
        file_id = self._require_allowed_file(file_id)
        response = await self._request(
            "GET", f"/files/{quote(file_id, safe='')}", params={"fields": FILE_FIELDS}
        )
        metadata = BoxFileMetadata.from_api(_response_json(response))
        if metadata.id != file_id:
            raise BoxResponseError("Box returned a different file than requested")
        return metadata

    def exclusion_reason(self, metadata: BoxFileMetadata) -> str | None:
        """Metadata-only policy checks; content is never downloaded for excluded files."""

        if metadata.trashed_at is not None or (
            metadata.item_status is not None and metadata.item_status != "active"
        ):
            return "file is trashed or not active"
        if metadata.extension not in self._allowed_extensions:
            return f"extension .{metadata.extension or '?'} is not allowed"
        if metadata.size > self._max_file_bytes:
            return f"size {metadata.size} bytes exceeds the {self._max_file_bytes}-byte cap"
        if metadata.size == 0:
            return "file is empty"
        return None

    @asynccontextmanager
    async def download_box_file(self, metadata: BoxFileMetadata) -> AsyncIterator[Path]:
        """Yield a private temporary copy of the exact metadata version; always deleted."""

        file_id = self._require_allowed_file(metadata.id)
        params = {"version": metadata.version_id} if metadata.version_id else None
        location = await self._download_location(file_id, params)
        descriptor, name = tempfile.mkstemp(prefix="box-", dir=self._temp_dir)
        path = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                digest = await self._stream_to(location, handle, file_id)
            if metadata.sha1 and digest != metadata.sha1.lower():
                raise BoxIncompleteContentError(
                    "Downloaded bytes do not match Box SHA-1 (file changed during download?)"
                )
            yield path
        finally:
            path.unlink(missing_ok=True)

    async def fetch_and_normalize_file(self, file_id: str) -> NormalizedDocument:
        metadata = await self.fetch_box_metadata(file_id)
        reason = self.exclusion_reason(metadata)
        if reason:
            raise BoxFileExcludedError(metadata.id, reason)
        async with self.download_box_file(metadata) as path:
            try:
                # CPU-bound parsing runs off the event loop.
                result = await asyncio.to_thread(self._extractor, path, metadata.extension)
            except BoxExtractionError as error:
                raise BoxIncompleteContentError(str(error)) from error
        return self._normalize(metadata, result)

    # ------------------------------------------------------------ internals

    def _normalize(self, metadata: BoxFileMetadata, result: ExtractionResult) -> NormalizedDocument:
        mime_type, _ = mimetypes.guess_type(metadata.name)
        return NormalizedDocument(
            source="box",
            external_id=metadata.id,
            title=metadata.name,
            source_url=BOX_WEB_FILE_URL.format(file_id=metadata.id),
            source_version=metadata.version_id,
            modified_at=metadata.modified_at,
            sections=result.sections,
            metadata={
                "box_account": self._account_label,
                "document_kind": "file",
                "filename": metadata.name,
                "extension": metadata.extension,
                "mime_type": mime_type,
                "size_bytes": metadata.size,
                "sha1": metadata.sha1,
                "etag": metadata.etag,
                "parent_folder_id": metadata.parent_id,
                "lifecycle_status": "active",
                **result.metadata(),
            },
        )

    async def _download_location(self, file_id: str, params: dict[str, str] | None) -> str:
        path = f"/files/{quote(file_id, safe='')}/content"
        for attempt in range(self._max_retries + 1):
            response = await self._request("GET", path, params=params, accept_redirect=True)
            if response.status_code == 302:
                location = response.headers.get("Location")
                if not location or urlsplit(location).scheme != "https":
                    raise BoxResponseError("Box download redirect has no HTTPS location")
                return location
            if response.status_code == 202 and attempt < self._max_retries:
                await self._sleep(self._retry_delay(response, attempt))
                continue
            if response.status_code == 202:
                raise BoxTransportError("Box file was not ready for download after retries")
            raise BoxResponseError(
                f"Unexpected Box download response HTTP {response.status_code}"
            )
        raise AssertionError("download loop exited unexpectedly")

    async def _stream_to(self, location: str, handle, file_id: str) -> str:
        """Fetch the signed URL without the Box Authorization header."""

        for attempt in range(self._max_retries + 1):
            digest = hashlib.sha1(usedforsecurity=False)
            written = 0
            handle.seek(0)
            handle.truncate()
            try:
                async with self._client.stream(
                    "GET", location, timeout=self._timeout, follow_redirects=True
                ) as response:
                    if response.status_code in RETRYABLE_STATUS_CODES and (
                        attempt < self._max_retries
                    ):
                        await self._sleep(self._retry_delay(response, attempt))
                        continue
                    if not response.is_success:
                        raise BoxAPIError(
                            status_code=response.status_code,
                            code="download_failed",
                            message="signed download URL request failed",
                        )
                    async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                        written += len(chunk)
                        if written > self._max_file_bytes:
                            raise BoxFileExcludedError(
                                file_id, "download exceeded the size cap while streaming"
                            )
                        digest.update(chunk)
                        handle.write(chunk)
                handle.flush()
                return digest.hexdigest()
            except httpx.TransportError as error:
                if attempt >= self._max_retries:
                    raise BoxTransportError("Box download failed after retries") from error
                await self._sleep(self._backoff_delay(attempt))
        raise BoxTransportError("Box download failed after retries")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        accept_redirect: bool = False,
    ) -> httpx.Response:
        refreshed = False
        attempt = 0
        while True:
            token = await self._auth.get_access_token()
            try:
                response = await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=self._timeout,
                    follow_redirects=False,
                )
            except httpx.TransportError as error:
                if attempt >= self._max_retries:
                    raise BoxTransportError("Box request failed after retries") from error
                await self._sleep(self._backoff_delay(attempt))
                attempt += 1
                continue

            if response.status_code == 401:
                if refreshed:
                    raise BoxAuthError("Box rejected the access token after a refresh")
                await self._auth.invalidate()
                refreshed = True
                continue
            if response.is_success or (accept_redirect and response.status_code == 302):
                return response
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                await self._sleep(self._retry_delay(response, attempt))
                attempt += 1
                continue
            raise _api_error(response)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), self._max_retry_delay)
            except ValueError:
                pass
        return self._backoff_delay(attempt)

    def _backoff_delay(self, attempt: int) -> float:
        base = min(2**attempt, self._max_retry_delay)
        return min(base + self._jitter(base), self._max_retry_delay)

    def _require_allowed_file(self, file_id: str) -> str:
        canonical = _canonicalize_box_id(file_id)
        if canonical not in self._allowed_file_ids:
            raise BoxSourceNotAllowedError(f"Box file is not approved: {canonical}")
        return canonical


def _canonicalize_box_id(value: str) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,30}", text):
        raise ValueError(f"invalid Box file ID: {value!r}")
    return text.lstrip("0") or "0"


def _api_error(response: httpx.Response) -> BoxAPIError:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    code = str(payload.get("code") or "unknown_error")
    message = str(payload.get("message") or "Box request failed")
    error_type = {403: BoxAccessLostError, 404: BoxNotFoundError}.get(
        response.status_code, BoxAPIError
    )
    return error_type(status_code=response.status_code, code=code, message=message)


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise BoxResponseError("Box returned a non-JSON response") from error
    if not isinstance(payload, dict):
        raise BoxResponseError("Box returned a non-object JSON response")
    return payload


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BoxResponseError("Box returned an invalid timestamp") from error
    if parsed.tzinfo is None:
        raise BoxResponseError("Box returned a timestamp without a timezone")
    return parsed
