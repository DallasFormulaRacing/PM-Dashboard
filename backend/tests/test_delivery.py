import json

import httpx
import pytest
from sqlalchemy import select

from app.messaging import service
from app.models import DeliveredVia, DiscordChannel, MessageStatus, OutboundMessage, User


def body(call) -> dict:
    return json.loads(call.request.content)


def only_row(session_factory, **filters) -> OutboundMessage:
    with session_factory() as s:
        return s.scalars(select(OutboundMessage).filter_by(**filters)).one()


def reload(session_factory, model, key):
    with session_factory() as s:
        return s.get(model, key)


@pytest.fixture
def announce(session, people, dispatched, settings):
    service.post_announcement(session, author=people["alice"], title="Car rollout", body="Saturday 9am.",
                              settings=settings)
    session.commit()
    return dispatched[-1]


@pytest.fixture
def assign(session, people, dispatched, settings):
    def _assign(who: str):
        task = service.create_task(session, creator=people["alice"], title="Wire harness", assignee=people[who],
                                   settings=settings)
        session.commit()
        return task, dispatched[-1]
    return _assign


# --- announcements: webhook posts under the sender's name -----------------------------

def test_announcement_posts_through_new_webhook_as_sender(deliverer, discord, session_factory, announce):
    create = discord.post("/channels/500/webhooks").respond(200, json={"id": "w1", "token": "tok"})
    execute = discord.post("/webhooks/w1/tok").respond(200, json={"id": "m1"})

    outcome = deliverer.deliver(_uuid(announce))

    assert outcome.status == MessageStatus.SENT
    assert create.called and execute.called
    sent = body(execute.calls.last)
    assert execute.calls.last.request.url.params["wait"] == "true"
    assert sent["username"] == "Alice Lead"
    assert sent["avatar_url"] == "https://cdn.example.com/a.png"
    assert "author" not in sent["embeds"][0]
    assert sent["embeds"][0]["footer"]["text"].startswith("PM Dashboard · #")
    assert sent["allowed_mentions"] == {"parse": []}

    row = only_row(session_factory)
    assert (row.discord_message_id, row.discord_channel_id, row.delivered_via) == ("m1", "500", DeliveredVia.WEBHOOK)
    assert reload(session_factory, DiscordChannel, "announcements").webhook_id == "w1"


def test_announcement_falls_back_to_bot_without_manage_webhooks(deliverer, discord, session_factory, announce):
    discord.post("/channels/500/webhooks").respond(403, json={"code": 50013, "message": "Missing Permissions"})
    post = discord.post("/channels/500/messages").respond(200, json={"id": "m2"})

    assert deliverer.deliver(_uuid(announce)).status == MessageStatus.SENT
    sent = body(post.calls.last)
    assert sent["enforce_nonce"] is True and len(sent["nonce"]) <= 25
    assert sent["embeds"][0]["author"]["name"] == "From Alice Lead"
    assert only_row(session_factory).delivered_via == DeliveredVia.BOT_CHANNEL


def test_webhook_retry_finds_earlier_post_instead_of_duplicating(deliverer, discord, session_factory, announce):
    with session_factory() as s:
        s.get(DiscordChannel, "announcements").webhook_id = "w1"
        s.get(DiscordChannel, "announcements").webhook_token = "tok"
        row = s.get(OutboundMessage, _uuid(announce))
        row.attempts = 1  # an earlier attempt timed out after Discord may have accepted it
        marker = row.marker
        s.commit()
    discord.get("/channels/500/messages").respond(200, json=[
        {"id": "other", "embeds": []},
        {"id": "m9", "embeds": [{"footer": {"text": f"PM Dashboard · {marker}"}}]},
    ])
    execute = discord.post("/webhooks/w1/tok").respond(200, json={"id": "dup"})

    assert deliverer.deliver(_uuid(announce)).status == MessageStatus.SENT
    assert not execute.called
    assert only_row(session_factory).discord_message_id == "m9"


def test_deleted_webhook_is_cleared_and_retried(deliverer, discord, session_factory, announce):
    with session_factory() as s:
        s.get(DiscordChannel, "announcements").webhook_id = "w1"
        s.get(DiscordChannel, "announcements").webhook_token = "tok"
        s.commit()
    discord.post("/webhooks/w1/tok").respond(404, json={"code": 10015, "message": "Unknown Webhook"})

    outcome = deliverer.deliver(_uuid(announce))
    assert outcome.status == MessageStatus.PENDING
    assert reload(session_factory, DiscordChannel, "announcements").webhook_id is None


def test_unconfigured_channel_fails(deliverer, session, people, dispatched, settings, session_factory):
    service.post_announcement(session, author=people["alice"], title="t", body="b", channel_purpose="nowhere",
                              settings=settings)
    session.commit()
    assert deliverer.deliver(_uuid(dispatched[-1])).status == MessageStatus.FAILED
    assert only_row(session_factory).last_error == "channel_not_configured: nowhere"


# --- task assignment DMs ----------------------------------------------------------------

def test_assignment_dm_opens_and_caches_channel(deliverer, discord, session_factory, assign, people):
    _, outbox_id = assign("bob")
    open_dm = discord.post("/users/@me/channels").respond(200, json={"id": "dm22"})
    post = discord.post("/channels/dm22/messages").respond(200, json={"id": "m3"})

    assert deliverer.deliver(_uuid(outbox_id)).status == MessageStatus.SENT
    assert body(open_dm.calls.last) == {"recipient_id": "222"}
    sent = body(post.calls.last)
    assert sent["embeds"][0]["title"] == "New task: Wire harness"
    assert sent["embeds"][0]["author"]["name"] == "From Alice Lead"
    assert sent["enforce_nonce"] is True
    assert reload(session_factory, User, people["bob"].id).discord_dm_channel_id == "dm22"
    row = only_row(session_factory)
    assert row.delivered_via == DeliveredVia.DM and not row.used_fallback


def test_unlinked_assignee_fails_without_calling_discord(deliverer, discord, session_factory, assign):
    _, outbox_id = assign("unlinked")
    assert deliverer.deliver(_uuid(outbox_id)).status == MessageStatus.FAILED
    assert only_row(session_factory).last_error == "not_linked"
    assert not discord.calls


def test_blocked_dms_fall_back_to_channel_with_mention(deliverer, discord, session_factory, assign, settings):
    settings.use_webhooks = False
    _, outbox_id = assign("bob")
    discord.post("/users/@me/channels").respond(200, json={"id": "dm22"})
    discord.post("/channels/dm22/messages").respond(403, json={"code": 50007, "message": "Cannot send"})
    fallback = discord.post("/channels/600/messages").respond(200, json={"id": "m4"})

    assert deliverer.deliver(_uuid(outbox_id)).status == MessageStatus.SENT
    sent = body(fallback.calls.last)
    assert sent["content"] == "<@222>"
    assert sent["allowed_mentions"] == {"parse": [], "users": ["222"]}
    row = only_row(session_factory)
    assert row.used_fallback and row.discord_channel_id == "600"


def test_stale_cached_dm_channel_is_reopened(deliverer, discord, session_factory, assign, people):
    with session_factory() as s:
        s.get(User, people["bob"].id).discord_dm_channel_id = "old"
        s.commit()
    _, outbox_id = assign("bob")
    discord.post("/channels/old/messages").respond(404, json={"code": 10003, "message": "Unknown Channel"})
    discord.post("/users/@me/channels").respond(200, json={"id": "new"})
    discord.post("/channels/new/messages").respond(200, json={"id": "m5"})

    assert deliverer.deliver(_uuid(outbox_id)).status == MessageStatus.SENT
    assert reload(session_factory, User, people["bob"].id).discord_dm_channel_id == "new"


# --- retries ------------------------------------------------------------------------------

def test_rate_limit_reschedules_without_using_an_attempt(deliverer, discord, session_factory, assign):
    _, outbox_id = assign("bob")
    discord.post("/users/@me/channels").respond(200, json={"id": "dm22"})
    discord.post("/channels/dm22/messages").respond(429, json={"retry_after": 2.5, "global": False})

    outcome = deliverer.deliver(_uuid(outbox_id))
    assert (outcome.status, outcome.retry_in) == (MessageStatus.PENDING, 2.5)
    row = only_row(session_factory)
    assert row.attempts == 0 and row.locked_at is None


def test_server_errors_back_off_then_die(deliverer, discord, session_factory, assign, settings):
    _, outbox_id = assign("bob")
    discord.post("/users/@me/channels").respond(200, json={"id": "dm22"})
    discord.post("/channels/dm22/messages").respond(502)

    retries = []
    for _ in range(settings.max_attempts):
        with session_factory() as s:  # make it due again
            s.get(OutboundMessage, _uuid(outbox_id)).available_at = _past()
            s.commit()
        retries.append(deliverer.deliver(_uuid(outbox_id)))

    assert [o.retry_in for o in retries[:2]] == [10, 30]
    assert retries[2].retry_in is None  # 120 s is past the countdown limit; the sweeper takes it
    assert retries[-1].status == MessageStatus.DEAD
    assert only_row(session_factory).attempts == settings.max_attempts


def test_network_timeout_is_retried(deliverer, discord, session_factory, assign):
    _, outbox_id = assign("bob")
    discord.post("/users/@me/channels").mock(side_effect=httpx.ReadTimeout("slow"))
    outcome = deliverer.deliver(_uuid(outbox_id))
    assert outcome.status == MessageStatus.PENDING
    assert "timeout" in only_row(session_factory).last_error


def test_missing_permissions_fail_permanently(deliverer, discord, session_factory, assign):
    _, outbox_id = assign("bob")
    discord.post("/users/@me/channels").respond(200, json={"id": "dm22"})
    discord.post("/channels/dm22/messages").respond(403, json={"code": 50013, "message": "Missing Permissions"})
    assert deliverer.deliver(_uuid(outbox_id)).status == MessageStatus.FAILED


def test_second_claim_does_nothing(deliverer, discord, session_factory, assign):
    _, outbox_id = assign("bob")
    discord.post("/users/@me/channels").respond(200, json={"id": "dm22"})
    post = discord.post("/channels/dm22/messages").respond(200, json={"id": "m6"})
    deliverer.deliver(_uuid(outbox_id))
    assert deliverer.deliver(_uuid(outbox_id)).status is None
    assert post.call_count == 1


def test_not_yet_due_row_is_not_claimed(deliverer, discord, session, people, dispatched, settings, session_factory):
    from datetime import timedelta

    from app.db import utcnow
    service.post_announcement(session, author=people["alice"], title="t", body="b",
                              scheduled_for=utcnow() + timedelta(hours=1), settings=settings)
    session.commit()
    row = only_row(session_factory)
    assert deliverer.deliver(row.id).status is None


# --- edits ------------------------------------------------------------------------------

def _deliver_assignment(deliverer, discord, assign, who="bob"):
    task, outbox_id = assign(who)
    discord.post("/users/@me/channels").respond(200, json={"id": "dm22"})
    discord.post("/channels/dm22/messages").respond(200, json={"id": "m7"})
    deliverer.deliver(_uuid(outbox_id))
    return task


def test_task_update_edits_existing_dm(deliverer, discord, session, people, dispatched, settings, assign):
    task = _deliver_assignment(deliverer, discord, assign)
    task = session.merge(task)
    task.title = "Wire harness v2"
    service.task_changed(session, task=task, actor=people["alice"], settings=settings)
    session.commit()
    edit = discord.patch("/channels/dm22/messages/m7").respond(200, json={"id": "m7"})

    assert deliverer.deliver(_uuid(dispatched[-1])).status == MessageStatus.SENT
    assert body(edit.calls.last)["embeds"][0]["title"] == "Task updated: Wire harness v2"


def test_task_update_sends_new_message_when_old_one_was_deleted(deliverer, discord, session, people, dispatched,
                                                                settings, assign):
    task = _deliver_assignment(deliverer, discord, assign)
    service.task_changed(session, task=session.merge(task), actor=people["alice"], settings=settings)
    session.commit()
    discord.patch("/channels/dm22/messages/m7").respond(404, json={"code": 10008, "message": "Unknown Message"})
    discord.post("/channels/dm22/messages").respond(200, json={"id": "m8"})

    assert deliverer.deliver(_uuid(dispatched[-1])).status == MessageStatus.SENT


def test_reassignment_edits_old_dm(deliverer, discord, session, people, dispatched, settings, assign):
    task = _deliver_assignment(deliverer, discord, assign)
    service.assign_task(session, task=session.merge(task), assignee=people["carol"], actor=people["alice"],
                        settings=settings)
    session.commit()
    unassigned_id, _ = dispatched[-2], dispatched[-1]
    edit = discord.patch("/channels/dm22/messages/m7").respond(200, json={"id": "m7"})

    assert deliverer.deliver(_uuid(unassigned_id)).status == MessageStatus.SENT
    assert body(edit.calls.last)["embeds"][0]["description"] == "Reassigned to Carol."


def test_unassign_without_earlier_message_is_skipped(deliverer, session, people, dispatched, settings, assign):
    task, _ = assign("unlinked")
    service.assign_task(session, task=session.merge(task), assignee=None, actor=people["alice"], settings=settings)
    session.commit()
    assert deliverer.deliver(_uuid(dispatched[-1])).status == MessageStatus.SKIPPED


def _uuid(value: str):
    import uuid
    return uuid.UUID(value)


def _past():
    from datetime import timedelta

    from app.db import utcnow
    return utcnow() - timedelta(seconds=1)
