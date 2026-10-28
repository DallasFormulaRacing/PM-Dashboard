"""Minimal synchronous Discord REST client: only the calls the pipeline makes.

No gateway connection; messages only flow dashboard -> Discord.
"""

import logging
import time
from typing import Any

import httpx

from app.discord.errors import DiscordAPIError, RateLimited, TransientDiscordError

log = logging.getLogger(__name__)

API_VERSION_BASE = "https://discord.com/api/v10"


class DiscordClient:
    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = API_VERSION_BASE,
        user_agent_url: str = "https://github.com/pm-dashboard",
        timeout: float = 10.0,
        max_inline_wait: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": f"DiscordBot ({user_agent_url}, 0.1)"},
        )
        self._auth = {"Authorization": f"Bot {bot_token}"}
        self._max_inline_wait = max_inline_wait
        # route key -> monotonic time its bucket resets, recorded when Discord reports 0 remaining
        self._exhausted_until: dict[str, float] = {}

    def close(self) -> None:
        self._http.close()

    # --- bot endpoints -------------------------------------------------------------

    def get_user(self, user_id: str) -> dict[str, Any]:
        return self._request("GET", f"/users/{user_id}", route="GET /users")

    def create_dm_channel(self, user_id: str) -> str:
        data = self._request("POST", "/users/@me/channels", route="POST /users/@me/channels",
                             json={"recipient_id": user_id})
        return data["id"]

    def create_message(self, channel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/channels/{channel_id}/messages",
                             route=f"POST /channels/{channel_id}/messages", json=payload)

    def edit_message(self, channel_id: str, message_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/channels/{channel_id}/messages/{message_id}",
                             route=f"PATCH /channels/{channel_id}/messages", json=payload)

    def recent_messages(self, channel_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._request("GET", f"/channels/{channel_id}/messages",
                             route=f"GET /channels/{channel_id}/messages", params={"limit": limit})

    def create_webhook(self, channel_id: str, name: str) -> dict[str, Any]:
        return self._request("POST", f"/channels/{channel_id}/webhooks",
                             route=f"POST /channels/{channel_id}/webhooks", json={"name": name})

    # --- webhook endpoints (authenticated by the webhook token, not the bot) -------

    def execute_webhook(self, webhook_id: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/webhooks/{webhook_id}/{token}", route=f"POST /webhooks/{webhook_id}",
                             params={"wait": "true"}, json=payload, auth=False)

    def edit_webhook_message(self, webhook_id: str, token: str, message_id: str,
                             payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/webhooks/{webhook_id}/{token}/messages/{message_id}",
                             route=f"PATCH /webhooks/{webhook_id}/messages", json=payload, auth=False)

    # --- transport ------------------------------------------------------------------

    def _request(self, method: str, path: str, *, route: str, json: Any = None,
                 params: dict[str, Any] | None = None, auth: bool = True) -> Any:
        self._wait_for_bucket(route)
        try:
            resp = self._http.request(method, path, json=json, params=params,
                                      headers=self._auth if auth else None)
        except httpx.TimeoutException as exc:
            # A read timeout may mean Discord processed the request; a connect timeout means it didn't.
            raise TransientDiscordError(f"{route}: timeout ({type(exc).__name__})",
                                        ambiguous=not isinstance(exc, httpx.ConnectTimeout)) from exc
        except httpx.TransportError as exc:
            raise TransientDiscordError(f"{route}: {type(exc).__name__}: {exc}",
                                        ambiguous=not isinstance(exc, httpx.ConnectError)) from exc

        self._record_bucket(route, resp)

        if resp.status_code == 429:
            body = _json_or_empty(resp)
            retry_after = float(body.get("retry_after") or resp.headers.get("Retry-After") or 1.0)
            raise RateLimited(retry_after, is_global=bool(body.get("global")))
        if resp.status_code >= 500:
            raise TransientDiscordError(f"{route}: HTTP {resp.status_code}", ambiguous=True)
        if resp.status_code >= 400:
            body = _json_or_empty(resp)
            raise DiscordAPIError(resp.status_code, body.get("code"), body.get("message") or resp.text[:200])
        if resp.status_code == 204:
            return None
        return resp.json()

    def _wait_for_bucket(self, route: str) -> None:
        reset_at = self._exhausted_until.get(route)
        if reset_at is None:
            return
        wait = reset_at - time.monotonic()
        if wait <= 0:
            self._exhausted_until.pop(route, None)
        elif wait <= self._max_inline_wait:
            time.sleep(wait)
            self._exhausted_until.pop(route, None)
        else:
            # Don't hold a worker slot; let the pipeline reschedule.
            raise RateLimited(wait)

    def _record_bucket(self, route: str, resp: httpx.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset_after = resp.headers.get("X-RateLimit-Reset-After")
        if remaining == "0" and reset_after:
            self._exhausted_until[route] = time.monotonic() + float(reset_after)
        elif remaining is not None:
            self._exhausted_until.pop(route, None)


def _json_or_empty(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
