"""Box OAuth token handling: rotation, persistence, concurrency, and secret hygiene."""

import asyncio
from pathlib import Path
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import Settings
from app.connectors.box_auth import (
    BoxAuthError,
    DeveloperTokenAuth,
    OAuthTokens,
    OAuthTokenStore,
    OAuthUserAuth,
    build_authorize_url,
    exchange_authorization_code,
)


SECRET = "synthetic-client-secret"


def run(coroutine):
    return asyncio.run(coroutine)


def make_auth(tmp_path: Path, handler, *, now: float = 1000.0, tokens: OAuthTokens | None = None):
    store = OAuthTokenStore(tmp_path / "tokens.json")
    if tokens is not None:
        store.save(tokens)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = OAuthUserAuth(
        client_id="synthetic-client-id",
        client_secret=SECRET,
        store=store,
        client=client,
        clock=lambda: now,
    )
    return auth, store


def token_response(n: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": f"access-{n}", "refresh_token": f"refresh-{n}", "expires_in": 3600},
    )


def test_store_writes_owner_only_file_atomically(tmp_path: Path) -> None:
    store = OAuthTokenStore(tmp_path / "nested" / "tokens.json")
    store.save(OAuthTokens("a", "r", 123.0))
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == 0o600
    assert store.load() == OAuthTokens("a", "r", 123.0)
    assert [p.name for p in store.path.parent.iterdir()] == ["tokens.json"]


def test_missing_tokens_point_to_login_script(tmp_path: Path) -> None:
    auth, _ = make_auth(tmp_path, lambda _: token_response(1))
    with pytest.raises(BoxAuthError, match="box_oauth_login.py"):
        run(auth.get_access_token())


def test_valid_token_is_used_without_refresh(tmp_path: Path) -> None:
    calls = []
    auth, _ = make_auth(
        tmp_path,
        lambda request: calls.append(request) or token_response(9),
        tokens=OAuthTokens("access-0", "refresh-0", expires_at=5000.0),
    )
    assert run(auth.get_access_token()) == "access-0"
    assert calls == []


def test_expired_token_refreshes_and_persists_rotated_refresh_token(tmp_path: Path) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return token_response(1)

    auth, store = make_auth(
        tmp_path, handler, tokens=OAuthTokens("access-0", "refresh-0", expires_at=1010.0)
    )
    assert run(auth.get_access_token()) == "access-1"
    assert seen["grant_type"] == "refresh_token" and seen["refresh_token"] == "refresh-0"
    assert store.load().refresh_token == "refresh-1"


def test_concurrent_callers_spend_the_single_use_refresh_token_once(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return token_response(len(calls))

    auth, _ = make_auth(tmp_path, handler, tokens=OAuthTokens("old", "refresh-0", expires_at=0.0))

    async def many():
        return await asyncio.gather(*(auth.get_access_token() for _ in range(5)))

    assert run(many()) == ["access-1"] * 5
    assert len(calls) == 1


def test_invalidate_forces_one_refresh(tmp_path: Path) -> None:
    calls = []
    auth, _ = make_auth(
        tmp_path,
        lambda request: calls.append(request) or token_response(len(calls)),
        tokens=OAuthTokens("access-0", "refresh-0", expires_at=9999.0),
    )

    async def flow():
        await auth.get_access_token()
        await auth.invalidate()
        first = await auth.get_access_token()
        second = await auth.get_access_token()
        return first, second

    assert run(flow()) == ("access-1", "access-1")
    assert len(calls) == 1


def test_rejected_refresh_asks_for_login_and_hides_secrets(tmp_path: Path) -> None:
    auth, store = make_auth(
        tmp_path,
        lambda _: httpx.Response(400, json={"error": "invalid_grant"}),
        tokens=OAuthTokens("access-0", "refresh-0", expires_at=0.0),
    )
    with pytest.raises(BoxAuthError) as info:
        run(auth.get_access_token())
    message = str(info.value)
    assert "box_oauth_login.py" in message
    assert SECRET not in message and "refresh-0" not in message
    assert store.load().refresh_token == "refresh-0"  # unchanged on failure


def test_developer_token_cannot_refresh() -> None:
    auth = DeveloperTokenAuth("synthetic-dev-token")

    async def flow():
        assert await auth.get_access_token() == "synthetic-dev-token"
        await auth.invalidate()
        await auth.get_access_token()

    with pytest.raises(BoxAuthError, match="60 minutes"):
        run(flow())


def test_authorize_url_carries_state_and_exact_redirect() -> None:
    url = build_authorize_url(
        client_id="cid", redirect_uri="http://localhost:8765/callback", state="xyz"
    )
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).netloc == "account.box.com"
    assert query == {
        "response_type": ["code"],
        "client_id": ["cid"],
        "redirect_uri": ["http://localhost:8765/callback"],
        "state": ["xyz"],
    }


def test_code_exchange_persists_tokens(tmp_path: Path) -> None:
    store = OAuthTokenStore(tmp_path / "tokens.json")

    async def flow():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: token_response(1))
        ) as client:
            return await exchange_authorization_code(
                client=client,
                code="synthetic-code",
                client_id="cid",
                client_secret=SECRET,
                redirect_uri="http://localhost:8765/callback",
                store=store,
                clock=lambda: 0.0,
            )

    tokens = run(flow())
    assert tokens.expires_at == 3600.0
    assert store.load() == tokens


def test_settings_parse_comma_separated_box_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOX_ALLOWED_FILE_IDS", " 123, 456 ,,789 ")
    monkeypatch.setenv("BOX_ALLOWED_EXTENSIONS", "pdf,md")
    settings = Settings(_env_file=None)
    assert settings.box_allowed_file_ids == ["123", "456", "789"]
    assert settings.box_allowed_extensions == ["pdf", "md"]
