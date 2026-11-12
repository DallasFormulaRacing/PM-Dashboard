"""Contract tests for the allowlist-enforcing, read-only Box connector (mocked HTTP)."""

import asyncio
from collections.abc import Callable
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.connectors.box import (
    BoxAccessLostError,
    BoxConnector,
    BoxFileExcludedError,
    BoxIncompleteContentError,
    BoxNotFoundError,
    BoxResponseError,
    BoxSourceNotAllowedError,
)
from app.connectors.box_auth import BoxAuthError
from tests.unit.box_helpers import FakeAuth, make_pdf


FILE_ID = "1001"
OTHER_ID = "2002"
TOKEN = "synthetic-access-token"
DOWNLOAD_URL = "https://dl.boxcloud.example/d/1/signed?sig=synthetic"
PDF_BYTES = make_pdf([["Synthetic battery limit is 600 volts."], ["Second synthetic page."]])


def metadata(**overrides: Any) -> dict[str, Any]:
    payload = {
        "type": "file",
        "id": FILE_ID,
        "name": "Synthetic Rules.pdf",
        "extension": "pdf",
        "size": len(PDF_BYTES),
        "sha1": hashlib.sha1(PDF_BYTES).hexdigest(),
        "etag": "3",
        "file_version": {"type": "file_version", "id": "555"},
        "modified_at": "2026-10-21T12:30:00-07:00",
        "trashed_at": None,
        "item_status": "active",
        "parent": {"type": "folder", "id": "77"},
    }
    payload.update(overrides)
    return payload


def run(coroutine):
    return asyncio.run(coroutine)


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def make(
    handler: Callable[[httpx.Request], httpx.Response],
    tmp_path: Path,
    *,
    auth: FakeAuth | None = None,
    recorder: Recorder | None = None,
    **options: Any,
) -> tuple[BoxConnector, Recorder]:
    recorder = recorder or Recorder()

    def recording(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        return handler(request)

    connector = BoxConnector(
        auth=auth or FakeAuth(),
        account_label="synthetic-test",
        allowed_file_ids=[FILE_ID],
        client=httpx.AsyncClient(transport=httpx.MockTransport(recording)),
        owns_client=True,
        sleep=recorder.sleep,
        jitter=lambda _: 0,
        temp_dir=tmp_path,
        **options,
    )
    return connector, recorder


def happy_handler(meta: dict[str, Any] | None = None, body: bytes = PDF_BYTES):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.box.com" and request.url.path == f"/2.0/files/{FILE_ID}":
            return httpx.Response(200, json=meta or metadata())
        if request.url.path == f"/2.0/files/{FILE_ID}/content":
            return httpx.Response(302, headers={"Location": DOWNLOAD_URL})
        if request.url.host == "dl.boxcloud.example":
            return httpx.Response(200, content=body)
        return httpx.Response(500, json={"code": "unexpected"})

    return handler


async def fetch(connector: BoxConnector, file_id: str = FILE_ID):
    async with connector:
        return await connector.fetch_and_normalize_file(file_id)


def test_unapproved_file_is_rejected_before_any_request(tmp_path: Path) -> None:
    connector, recorder = make(happy_handler(), tmp_path)
    with pytest.raises(BoxSourceNotAllowedError):
        run(fetch(connector, OTHER_ID))
    assert recorder.requests == []


@pytest.mark.parametrize("bad_id", ["", "abc", "12 34", "../1001", "1001/content"])
def test_malformed_ids_are_rejected(tmp_path: Path, bad_id: str) -> None:
    connector, recorder = make(happy_handler(), tmp_path)
    with pytest.raises(ValueError):
        run(fetch(connector, bad_id))
    assert recorder.requests == []


def test_happy_path_normalizes_with_canonical_citation(tmp_path: Path) -> None:
    connector, recorder = make(happy_handler(), tmp_path)
    document = run(fetch(connector))

    assert document.source == "box"
    assert document.external_id == FILE_ID
    assert document.title == "Synthetic Rules.pdf"
    assert document.source_url == f"https://app.box.com/file/{FILE_ID}"
    assert document.source_version == "555"
    assert document.modified_at is not None and document.modified_at.utcoffset() is not None
    assert [s.page_number for s in document.sections] == [1, 2]
    assert document.metadata["box_account"] == "synthetic-test"
    assert document.metadata["sha1"] == hashlib.sha1(PDF_BYTES).hexdigest()
    assert document.metadata["parser"] == "pypdf"
    assert "signed" not in json.dumps(document.model_dump(mode="json"))

    api_calls = [r for r in recorder.requests if r.url.host == "api.box.com"]
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in api_calls)
    content_call = next(r for r in api_calls if r.url.path.endswith("/content"))
    assert content_call.url.params["version"] == "555"
    download = next(r for r in recorder.requests if r.url.host == "dl.boxcloud.example")
    assert "Authorization" not in download.headers
    assert list(tmp_path.iterdir()) == []  # temporary download deleted


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"name": "Deck.pptx", "extension": "pptx"}, "extension .pptx"),
        ({"trashed_at": "2026-10-22T00:00:00Z", "item_status": "trashed"}, "trashed"),
        ({"size": 10_000_000}, "exceeds"),
        ({"size": 0}, "empty"),
    ],
)
def test_policy_exclusions_never_download_content(
    tmp_path: Path, overrides: dict[str, Any], reason: str
) -> None:
    connector, recorder = make(happy_handler(metadata(**overrides)), tmp_path, max_file_bytes=1_000_000)
    with pytest.raises(BoxFileExcludedError, match=reason):
        run(fetch(connector))
    assert all(not r.url.path.endswith("/content") for r in recorder.requests)


def test_not_ready_202_waits_for_retry_after(tmp_path: Path) -> None:
    calls = {"content": 0}
    base = happy_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content"):
            calls["content"] += 1
            if calls["content"] == 1:
                return httpx.Response(202, headers={"Retry-After": "7"})
        return base(request)

    connector, recorder = make(handler, tmp_path)
    run(fetch(connector))
    assert recorder.sleeps == [7.0]


def test_rate_limit_429_honours_retry_after(tmp_path: Path) -> None:
    calls = {"meta": 0}
    base = happy_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/2.0/files/{FILE_ID}":
            calls["meta"] += 1
            if calls["meta"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3"}, json={"code": "rate_limit"})
        return base(request)

    connector, recorder = make(handler, tmp_path)
    run(fetch(connector))
    assert recorder.sleeps == [3.0]


def test_401_invalidates_and_retries_once(tmp_path: Path) -> None:
    auth = FakeAuth(["expired-token", TOKEN])
    base = happy_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") == "Bearer expired-token":
            return httpx.Response(401, json={"code": "unauthorized"})
        return base(request)

    connector, _ = make(handler, tmp_path, auth=auth)
    run(fetch(connector))
    assert auth.invalidations == 1


def test_repeated_401_raises_auth_error_without_token(tmp_path: Path) -> None:
    connector, _ = make(lambda _: httpx.Response(401, json={"code": "unauthorized"}), tmp_path)
    with pytest.raises(BoxAuthError) as info:
        run(fetch(connector))
    assert TOKEN not in str(info.value)


@pytest.mark.parametrize(("status", "error"), [(403, BoxAccessLostError), (404, BoxNotFoundError)])
def test_access_loss_and_not_found_are_typed(tmp_path: Path, status: int, error: type) -> None:
    connector, _ = make(
        lambda _: httpx.Response(status, json={"code": "denied", "message": "synthetic"}),
        tmp_path,
    )
    with pytest.raises(error) as info:
        run(fetch(connector))
    assert info.value.status_code == status


def test_sha1_mismatch_is_rejected_and_temp_deleted(tmp_path: Path) -> None:
    connector, _ = make(happy_handler(metadata(sha1="0" * 40)), tmp_path)
    with pytest.raises(BoxIncompleteContentError, match="SHA-1"):
        run(fetch(connector))
    assert list(tmp_path.iterdir()) == []


def test_stream_larger_than_cap_aborts(tmp_path: Path) -> None:
    big = b"x" * 5000
    connector, _ = make(
        happy_handler(metadata(size=100, sha1=None), body=big), tmp_path, max_file_bytes=1000
    )
    with pytest.raises(BoxFileExcludedError, match="size cap"):
        run(fetch(connector))
    assert list(tmp_path.iterdir()) == []


def test_unextractable_content_is_incomplete_not_empty_document(tmp_path: Path) -> None:
    scan = make_pdf([[]])
    meta = metadata(size=len(scan), sha1=hashlib.sha1(scan).hexdigest())
    connector, _ = make(happy_handler(meta, body=scan), tmp_path)
    with pytest.raises(BoxIncompleteContentError, match="no extractable text"):
        run(fetch(connector))
    assert list(tmp_path.iterdir()) == []


def test_redirect_without_https_location_is_rejected(tmp_path: Path) -> None:
    base = happy_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"Location": "http://insecure.example/file"})
        return base(request)

    connector, _ = make(handler, tmp_path)
    with pytest.raises(BoxResponseError):
        run(fetch(connector))


def test_metadata_for_a_different_file_is_rejected(tmp_path: Path) -> None:
    connector, _ = make(happy_handler(metadata(id=OTHER_ID)), tmp_path)
    with pytest.raises(BoxResponseError, match="different file"):
        run(fetch(connector))


def test_unsupported_allowed_extension_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pptx"):
        make(happy_handler(), tmp_path, allowed_extensions=["pdf", "pptx"])
