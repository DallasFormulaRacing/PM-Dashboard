import pytest

from app.discord import DiscordAPIError, RateLimited, TransientDiscordError


def test_sends_bot_auth_and_user_agent(client, discord):
    route = discord.post("/channels/1/messages").respond(200, json={"id": "m"})
    client.create_message("1", {"content": "hi"})
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bot test-token"
    assert req.headers["User-Agent"].startswith("DiscordBot (")


def test_webhook_calls_do_not_send_bot_token(client, discord):
    route = discord.post("/webhooks/w/t").respond(200, json={"id": "m"})
    client.execute_webhook("w", "t", {"content": "hi"})
    assert "Authorization" not in route.calls.last.request.headers


def test_429_raises_rate_limited(client, discord):
    discord.post("/channels/1/messages").respond(429, json={"retry_after": 1.25, "global": True})
    with pytest.raises(RateLimited) as exc:
        client.create_message("1", {})
    assert exc.value.retry_after == 1.25 and exc.value.is_global


def test_exhausted_bucket_raises_before_calling(client, discord):
    route = discord.post("/channels/1/messages").respond(
        200, json={"id": "m"}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "30"})
    client.create_message("1", {})
    with pytest.raises(RateLimited):
        client.create_message("1", {})
    assert route.call_count == 1


def test_5xx_is_transient_and_4xx_is_api_error(client, discord):
    discord.post("/channels/1/messages").respond(503)
    with pytest.raises(TransientDiscordError):
        client.create_message("1", {})
    discord.post("/channels/2/messages").respond(403, json={"code": 50007, "message": "Cannot send"})
    with pytest.raises(DiscordAPIError) as exc:
        client.create_message("2", {})
    assert exc.value.code == 50007
