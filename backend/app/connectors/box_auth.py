"""Box access-token providers for the read-only Box connector.

Test track: a 60-minute developer token, or an OAuth 2.0 user login whose tokens
live in a git-ignored local file. Box refresh tokens are single-use and rotate on
every refresh, so the rotated token is persisted atomically before it is used.
Secrets never appear in exception messages or logs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from typing import Protocol
from urllib.parse import urlencode

import httpx


BOX_AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize"
BOX_TOKEN_URL = "https://api.box.com/oauth2/token"
# Refresh slightly early so a token does not expire mid-request.
EXPIRY_MARGIN_SECONDS = 60


class BoxAuthError(RuntimeError):
    """Authentication is missing, expired, or rejected; never includes secrets."""


class BoxAuth(Protocol):
    async def get_access_token(self) -> str: ...

    async def invalidate(self) -> None: ...


class DeveloperTokenAuth:
    """Fixed developer token: valid for 60 minutes and cannot be refreshed."""

    def __init__(self, token: str) -> None:
        if not token.strip():
            raise BoxAuthError("BOX_DEVELOPER_TOKEN is empty")
        self._token = token.strip()
        self._invalid = False

    async def get_access_token(self) -> str:
        if self._invalid:
            raise BoxAuthError(
                "Box developer token was rejected (expired after 60 minutes?); "
                "generate a new one in the Developer Console"
            )
        return self._token

    async def invalidate(self) -> None:
        self._invalid = True


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds

    @classmethod
    def from_token_response(cls, payload: object, *, now: float) -> OAuthTokens:
        if not isinstance(payload, dict):
            raise BoxAuthError("Box token endpoint returned a non-object response")
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise BoxAuthError("Box token endpoint response is missing tokens")
        if not isinstance(expires_in, int | float) or expires_in <= 0:
            raise BoxAuthError("Box token endpoint response has no valid expires_in")
        return cls(access, refresh, now + float(expires_in))

    def to_json(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }


class OAuthTokenStore:
    """JSON token file with owner-only permissions and atomic replacement."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> OAuthTokens | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise BoxAuthError(f"Box token file is unreadable: {self.path}") from error
        try:
            return OAuthTokens(
                access_token=str(raw["access_token"]),
                refresh_token=str(raw["refresh_token"]),
                expires_at=float(raw["expires_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise BoxAuthError(f"Box token file is malformed: {self.path}") from error

    def save(self, tokens: OAuthTokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".box_tokens.", suffix=".tmp", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(tokens.to_json(), handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise


class OAuthUserAuth:
    """OAuth 2.0 user tokens: refresh on expiry or after a 401, one refresh at a time."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        store: OAuthTokenStore,
        client: httpx.AsyncClient,
        token_url: str = BOX_TOKEN_URL,
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not client_id.strip() or not client_secret.strip():
            raise BoxAuthError("BOX_CLIENT_ID and BOX_CLIENT_SECRET are required for OAuth")
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._store = store
        self._client = client
        self._token_url = token_url
        self._timeout = timeout_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._tokens: OAuthTokens | None = None
        self._force_refresh = False

    async def get_access_token(self) -> str:
        async with self._lock:
            if self._tokens is None:
                self._tokens = self._store.load()
                if self._tokens is None:
                    raise BoxAuthError(
                        "No Box OAuth tokens found; run scripts/box_oauth_login.py first"
                    )
            if self._force_refresh or self._clock() >= (
                self._tokens.expires_at - EXPIRY_MARGIN_SECONDS
            ):
                self._tokens = await self._refresh(self._tokens)
                self._force_refresh = False
            return self._tokens.access_token

    async def invalidate(self) -> None:
        async with self._lock:
            self._force_refresh = True

    async def _refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        try:
            response = await self._client.post(
                self._token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=self._timeout,
            )
        except httpx.TransportError as error:
            raise BoxAuthError("Box token refresh failed: network error") from error
        if response.status_code in (400, 401):
            raise BoxAuthError(
                "Box refresh token was rejected (expired, revoked, or already used); "
                "run scripts/box_oauth_login.py again"
            )
        if not response.is_success:
            raise BoxAuthError(f"Box token refresh failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise BoxAuthError("Box token endpoint returned non-JSON") from error
        rotated = OAuthTokens.from_token_response(payload, now=self._clock())
        # Persist before use: the previous refresh token is now dead.
        self._store.save(rotated)
        return rotated


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{BOX_AUTHORIZE_URL}?{query}"


def new_state() -> str:
    return secrets.token_urlsafe(32)


async def exchange_authorization_code(
    *,
    client: httpx.AsyncClient,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    store: OAuthTokenStore,
    token_url: str = BOX_TOKEN_URL,
    clock: Callable[[], float] = time.time,
) -> OAuthTokens:
    """Exchange a 30-second authorization code for tokens and persist them."""

    try:
        response = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            timeout=30.0,
        )
    except httpx.TransportError as error:
        raise BoxAuthError("Box code exchange failed: network error") from error
    if not response.is_success:
        raise BoxAuthError(
            f"Box code exchange failed with HTTP {response.status_code} "
            "(code expired after 30 seconds, or redirect URI mismatch?)"
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise BoxAuthError("Box token endpoint returned non-JSON") from error
    tokens = OAuthTokens.from_token_response(payload, now=clock())
    store.save(tokens)
    return tokens
