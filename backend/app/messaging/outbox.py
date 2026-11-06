"""Transactional outbox: rows are written in the caller's transaction and queued only after it commits.

If queuing fails (Redis down, connection limit), the row stays `pending` and the beat sweeper
picks it up, so a failed enqueue never loses a message.
"""

import logging
import uuid
from collections.abc import Callable

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import OutboundMessage

log = logging.getLogger(__name__)

_PENDING_KEY = "outbox_dispatch_ids"

Dispatcher = Callable[[str], None]
_dispatcher: Dispatcher | None = None


def set_dispatcher(dispatcher: Dispatcher | None) -> None:
    """Install the function that queues a delivery job (Celery in production, a stub in tests)."""
    global _dispatcher
    _dispatcher = dispatcher


def celery_dispatcher(outbox_id: str) -> None:
    from app.celery_client import get_celery

    # By task name, so the API never imports worker code.
    get_celery().send_task("discord.deliver", args=[outbox_id], queue="discord")


def add(session: Session, message: OutboundMessage) -> OutboundMessage:
    if message.id is None:
        message.id = uuid.uuid4()
    if message.available_at is None:
        message.available_at = utcnow()
    session.add(message)
    # Scheduled rows are released by the sweeper when they come due.
    if message.available_at <= utcnow():
        session.info.setdefault(_PENDING_KEY, []).append(str(message.id))
    return message


@event.listens_for(Session, "after_commit")
def _dispatch_after_commit(session: Session) -> None:
    ids = session.info.pop(_PENDING_KEY, [])
    if not ids:
        return
    dispatcher = _dispatcher or celery_dispatcher
    for outbox_id in ids:
        try:
            dispatcher(outbox_id)
        except Exception:
            log.warning("enqueue failed for outbox %s; the sweeper will retry it", outbox_id, exc_info=True)


@event.listens_for(Session, "after_soft_rollback")
def _discard_on_rollback(session: Session, previous_transaction) -> None:
    session.info.pop(_PENDING_KEY, None)
