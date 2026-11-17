"""Async versions of `app.messaging.service` for the API. Same arguments and return values, but they
take an `AsyncSession`. Each one runs the sync function on the session's underlying sync session,
so there's one copy of the logic and the outbox still queues rows only after the endpoint commits:

    @router.post("/tasks")
    async def create(body: TaskIn, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
        task = await async_service.create_task(db, creator=user, title=body.title)
        await db.commit()  # outbox rows are queued to Celery here
        return task
"""

import functools
from collections.abc import Awaitable, Callable
from typing import Concatenate

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.messaging import service


def _on_async_session[**P, R](
    fn: Callable[Concatenate[Session, P], R],
) -> Callable[Concatenate[AsyncSession, P], Awaitable[R]]:
    @functools.wraps(fn)
    async def wrapper(session: AsyncSession, *args: P.args, **kwargs: P.kwargs) -> R:
        return await session.run_sync(fn, *args, **kwargs)

    return wrapper


post_announcement = _on_async_session(service.post_announcement)
create_task = _on_async_session(service.create_task)
assign_task = _on_async_session(service.assign_task)
task_changed = _on_async_session(service.task_changed)
