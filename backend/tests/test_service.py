from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import utcnow
from app.messaging import service
from app.messaging.condense import condense
from app.messaging.payloads import webhook_username
from app.models import MessageKind, MessageStatus, OutboundMessage, TargetType


def rows(session):
    return session.scalars(select(OutboundMessage).order_by(OutboundMessage.created_at)).all()


def test_announcement_writes_outbox_row_and_dispatches_after_commit(session, people, dispatched, settings):
    alice = people["alice"]
    ann = service.post_announcement(session, author=alice, title="Shop closed Friday", body="Back Monday.",
                                    settings=settings)
    assert dispatched == []  # nothing queued before commit
    session.commit()

    [msg] = rows(session)
    assert dispatched == [str(msg.id)]
    assert msg.kind == MessageKind.ANNOUNCEMENT
    assert msg.target_type == TargetType.CHANNEL
    assert msg.channel_purpose == "announcements"
    assert msg.sender_id == alice.id
    assert msg.status == MessageStatus.PENDING
    assert msg.payload["sender"] == {"name": "Alice Lead", "avatar_url": "https://cdn.example.com/a.png"}
    assert msg.payload["embed"]["title"] == "Shop closed Friday"
    assert msg.payload["embed"]["url"] == f"https://app.example.com/announcements/{ann.id}"


def test_rollback_dispatches_nothing(session, people, dispatched, settings):
    service.post_announcement(session, author=people["alice"], title="t", body="b", settings=settings)
    session.rollback()
    session.commit()
    assert dispatched == []
    assert rows(session) == []


def test_scheduled_announcement_waits_for_the_sweeper(session, people, dispatched, settings):
    service.post_announcement(session, author=people["alice"], title="t", body="b",
                              scheduled_for=utcnow() + timedelta(hours=2), settings=settings)
    session.commit()
    assert dispatched == []
    assert len(rows(session)) == 1


def test_naive_schedule_time_is_rejected(session, people, settings):
    with pytest.raises(ValueError):
        service.post_announcement(session, author=people["alice"], title="t", body="b",
                                  scheduled_for=utcnow().replace(tzinfo=None), settings=settings)


def test_reassignment_messages_both_people(session, people, dispatched, settings):
    alice, bob, carol = people["alice"], people["bob"], people["carol"]
    task = service.create_task(session, creator=alice, title="Torque spec sheet", assignee=bob, settings=settings)
    session.commit()
    service.assign_task(session, task=task, assignee=carol, actor=alice, settings=settings)
    session.commit()

    kinds = [(m.kind, m.recipient_user_id) for m in rows(session)]
    assert kinds == [
        (MessageKind.TASK_ASSIGNED, bob.id),
        (MessageKind.TASK_UNASSIGNED, bob.id),
        (MessageKind.TASK_ASSIGNED, carol.id),
    ]
    assert len(dispatched) == 3


def test_assigning_same_person_again_is_a_no_op(session, people, dispatched, settings):
    task = service.create_task(session, creator=people["alice"], title="t", assignee=people["bob"], settings=settings)
    assert service.assign_task(session, task=task, assignee=people["bob"], actor=people["alice"],
                               settings=settings) == []


def test_task_changed_only_messages_when_assigned(session, people, settings):
    task = service.create_task(session, creator=people["alice"], title="t", settings=settings)
    assert service.task_changed(session, task=task, actor=people["alice"], settings=settings) is None


def test_long_text_is_truncated_with_link():
    text = "word " * 100
    out = condense(text, link="https://x/1", threshold=50)
    assert out.endswith("[Read the full note](https://x/1)")
    assert len(out.split("\n\n")[0]) <= 51


def test_short_text_is_unchanged():
    assert condense("  hi  ", link="https://x/1", threshold=50) == "hi"


def test_summarizer_and_cached_summary_take_priority():
    class Fake:
        def summarize(self, text, *, max_chars):
            return "short version"

    long = "x" * 500
    assert condense(long, link="L", threshold=50, summarizer=Fake()).startswith("short version")
    assert condense(long, link="L", threshold=50, cached_summary="cached", summarizer=Fake()).startswith("cached")


@pytest.mark.parametrize("name,expected", [
    ("Alice", "Alice"),
    ("Discord Mod", "Mod"),
    ("clyde", "PM Dashboard"),
    ("x" * 100, "x" * 80),
])
def test_webhook_username_is_sanitized(name, expected):
    assert webhook_username(name) == expected
