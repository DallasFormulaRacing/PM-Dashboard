"""Periodic sweep run by Celery beat. Delivers anything the fast path missed:
- rows whose enqueue failed after commit, or whose Celery job was lost,
- scheduled announcements and long retries that have come due,
- rows stuck in `sending` because a worker died mid-send.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import utcnow
from app.models import MessageStatus, OutboundMessage, WorkerHeartbeat

log = logging.getLogger(__name__)


@dataclass
class SweepResult:
    released: int = 0
    dead: int = 0
    enqueued: list[str] = field(default_factory=list)


def sweep(session_factory: sessionmaker, dispatch: Callable[[str], None], settings: Settings,
          now: datetime | None = None) -> SweepResult:
    now = now or utcnow()
    result = SweepResult()

    with session_factory() as session:
        stuck = session.scalars(
            select(OutboundMessage)
            .where(OutboundMessage.status == MessageStatus.SENDING,
                   OutboundMessage.locked_at < now - timedelta(seconds=settings.stuck_after_seconds))
            .with_for_update(skip_locked=True)
        ).all()
        for msg in stuck:
            msg.attempts += 1
            msg.locked_at = None
            msg.last_error = "worker stopped mid-send"
            if msg.attempts >= settings.max_attempts:
                msg.status = MessageStatus.DEAD
                result.dead += 1
            else:
                msg.status = MessageStatus.PENDING
                msg.available_at = now
                result.released += 1

        last_queued = func.coalesce(OutboundMessage.enqueued_at, OutboundMessage.created_at)
        due = session.scalars(
            select(OutboundMessage.id)
            .where(OutboundMessage.status == MessageStatus.PENDING,
                   OutboundMessage.available_at <= now,
                   last_queued <= now - timedelta(seconds=settings.sweep_grace_seconds))
            .order_by(OutboundMessage.available_at)
            .limit(settings.sweep_batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        if due:
            session.execute(
                update(OutboundMessage).where(OutboundMessage.id.in_(due)).values(enqueued_at=now)
                .execution_options(synchronize_session=False)
            )
        session.merge(WorkerHeartbeat(name="beat", beat_at=now))
        session.commit()

    for outbox_id in due:
        try:
            dispatch(str(outbox_id))
            result.enqueued.append(str(outbox_id))
        except Exception:
            log.warning("sweeper failed to enqueue %s; will try next sweep", outbox_id, exc_info=True)

    if result.released or result.dead or result.enqueued:
        log.info("sweep: released=%d dead=%d enqueued=%d", result.released, result.dead, len(result.enqueued))
    return result
