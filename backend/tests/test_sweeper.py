from datetime import timedelta

from app.db import utcnow
from app.messaging import service
from app.models import MessageStatus, OutboundMessage, WorkerHeartbeat
from app.worker.sweeper import sweep


def _announce(session, people, settings, **kw):
    ann = service.post_announcement(session, author=people["alice"], title="t", body="b", settings=settings, **kw)
    session.commit()
    return session.query(OutboundMessage).filter_by(ref_id=ann.id).one()


def test_fresh_rows_are_left_to_the_fast_path(session_factory, session, people, settings, dispatched):
    _announce(session, people, settings)
    queued = []
    result = sweep(session_factory, queued.append, settings)
    assert queued == [] and result.enqueued == []


def test_rows_missed_by_the_fast_path_are_enqueued(session_factory, session, people, settings, dispatched):
    row = _announce(session, people, settings)
    queued = []
    later = utcnow() + timedelta(seconds=settings.sweep_grace_seconds + 1)
    sweep(session_factory, queued.append, settings, now=later)
    assert queued == [str(row.id)]

    # Marked as enqueued, so the next sweep leaves it alone for the grace period.
    queued.clear()
    sweep(session_factory, queued.append, settings, now=later + timedelta(seconds=5))
    assert queued == []


def test_scheduled_rows_are_released_when_due(session_factory, session, people, settings, dispatched):
    at = utcnow() + timedelta(hours=1)
    row = _announce(session, people, settings, scheduled_for=at)
    queued = []
    sweep(session_factory, queued.append, settings, now=at - timedelta(minutes=1))
    assert queued == []
    sweep(session_factory, queued.append, settings, now=at + timedelta(seconds=1))
    assert queued == [str(row.id)]


def test_stuck_sending_rows_are_released_or_killed(session_factory, session, people, settings, dispatched):
    row = _announce(session, people, settings)
    now = utcnow() + timedelta(minutes=10)
    with session_factory() as s:
        r = s.get(OutboundMessage, row.id)
        r.status, r.locked_at = MessageStatus.SENDING, now - timedelta(seconds=settings.stuck_after_seconds + 1)
        s.commit()

    queued = []
    result = sweep(session_factory, queued.append, settings, now=now)
    assert result.released == 1 and queued == [str(row.id)]
    with session_factory() as s:
        r = s.get(OutboundMessage, row.id)
        assert (r.status, r.attempts) == (MessageStatus.PENDING, 1)

        r.status, r.attempts, r.locked_at = MessageStatus.SENDING, settings.max_attempts - 1, r.available_at
        s.commit()
    result = sweep(session_factory, queued.append, settings, now=now + timedelta(hours=1))
    assert result.dead == 1


def test_failed_dispatch_is_tolerated_and_heartbeat_written(session_factory, session, people, settings, dispatched):
    _announce(session, people, settings)

    def broken(_):
        raise ConnectionError("redis down")

    result = sweep(session_factory, broken, settings, now=utcnow() + timedelta(minutes=5))
    assert result.enqueued == []
    with session_factory() as s:
        assert s.get(WorkerHeartbeat, "beat") is not None
