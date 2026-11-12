"""One-time Box OAuth 2.0 user login for the local test track.

Run from backend/: python scripts/box_oauth_login.py
Opens the Box consent page, receives the redirect on the configured localhost URI,
verifies `state`, exchanges the 30-second code immediately, and writes tokens to the
git-ignored BOX_OAUTH_TOKEN_PATH (mode 0600). Prints no tokens or secrets.
"""

from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.connectors.box_auth import (  # noqa: E402
    BoxAuthError,
    OAuthTokenStore,
    build_authorize_url,
    exchange_authorization_code,
    new_state,
)


def wait_for_code(redirect_uri: str, state: str, timeout_seconds: float) -> str:
    parts = urlsplit(redirect_uri)
    if parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1"}:
        raise SystemExit("BOX_OAUTH_REDIRECT_URI must be an http://localhost URI for this script")
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            request = urlsplit(self.path)
            if request.path != parts.path:
                self.send_response(404)
                self.end_headers()
                return
            query = parse_qs(request.query)
            if query.get("state", [""])[0] != state:
                result["error"] = "state mismatch (possible CSRF); try again"
            elif "error" in query:
                result["error"] = "Box returned: " + query["error"][0]
            elif "code" in query:
                result["code"] = query["code"][0]
            else:
                result["error"] = "redirect had no authorization code"
            ok = "code" in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Box login complete. You can close this tab."
                if ok
                else b"Box login failed. Check the terminal."
            )
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *_: object) -> None:  # keep the code out of logs
            return

    server = HTTPServer((parts.hostname, parts.port or 80), Handler)
    timer = threading.Timer(timeout_seconds, server.shutdown)
    timer.start()
    try:
        server.serve_forever()
    finally:
        timer.cancel()
        server.server_close()
    if "code" not in result:
        raise SystemExit(result.get("error", "timed out waiting for the Box redirect"))
    return result["code"]


async def exchange(settings: Settings, code: str) -> None:
    store = OAuthTokenStore(settings.box_oauth_token_path)
    async with httpx.AsyncClient() as client:
        await exchange_authorization_code(
            client=client,
            code=code,
            client_id=settings.box_client_id or "",
            client_secret=settings.box_client_secret.get_secret_value()
            if settings.box_client_secret
            else "",
            redirect_uri=settings.box_oauth_redirect_uri,
            store=store,
        )
        tokens = store.load()
        assert tokens is not None
        me = await client.get(
            "https://api.box.com/2.0/users/me",
            params={"fields": "id,enterprise"},
            headers={"Authorization": f"Bearer {tokens.access_token}"},
        )
    user_id = me.json().get("id") if me.is_success else "unknown"
    print(json.dumps({"status": "authorized", "box_user_id": user_id,
                      "token_file": str(settings.box_oauth_token_path)}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--no-browser", action="store_true", help="print the URL only")
    args = parser.parse_args()
    settings = Settings()
    if not settings.box_client_id or settings.box_client_secret is None:
        raise SystemExit("BOX_CLIENT_ID and BOX_CLIENT_SECRET must be set in .env")
    state = new_state()
    url = build_authorize_url(
        client_id=settings.box_client_id,
        redirect_uri=settings.box_oauth_redirect_uri,
        state=state,
    )
    print("Open this URL to authorize (it contains no secrets):", url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    code = wait_for_code(settings.box_oauth_redirect_uri, state, args.timeout)
    try:
        asyncio.run(exchange(settings, code))
    except BoxAuthError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
