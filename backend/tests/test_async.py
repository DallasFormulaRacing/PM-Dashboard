import os

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db
from app.db import Base, get_db, make_async_session_factory
from app.messaging import async_service
from app.models import MessageKind, OutboundMessage, User

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def async_session_factory():
    # Same switch as the sync engine fixture; TEST_DATABASE_URL must use postgresql+psycopg://.
    url = os.environ.get("TEST_DATABASE_URL")
    eng = create_async_engine(url) if url else create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_async_session_factory(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture
async def db(async_session_factory):
    async with async_session_factory() as s:
        yield s


@pytest.fixture
async def users(db):
    alice = User(name="Alice Lead", email="alice@example.com", avatar_url="https://cdn.example.com/a.png",
                 discord_user_id="111")
    bob = User(name="Bob", email="bob@example.com", discord_user_id="222")
    carol = User(name="Carol", email="carol@example.com", discord_user_id="333")
    db.add_all([alice, bob, carol])
    await db.commit()
    return {"alice": alice, "bob": bob, "carol": carol}


async def rows(session: AsyncSession) -> list[OutboundMessage]:
    return list(await session.scalars(select(OutboundMessage).order_by(OutboundMessage.created_at)))


async def test_announcement_dispatches_after_async_commit(db, users, dispatched, settings):
    ann = await async_service.post_announcement(db, author=users["alice"], title="Shop closed", body="Back Monday.",
                                                settings=settings)
    assert dispatched == []
    await db.commit()

    [msg] = await rows(db)
    assert dispatched == [str(msg.id)]
    assert msg.payload["embed"]["url"] == f"https://app.example.com/announcements/{ann.id}"


async def test_async_rollback_dispatches_nothing(db, users, dispatched, settings):
    await async_service.post_announcement(db, author=users["alice"], title="t", body="b", settings=settings)
    await db.rollback()
    await db.commit()
    assert dispatched == []
    assert await rows(db) == []


async def test_async_reassignment_messages_both_people(db, users, dispatched, settings):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    task = await async_service.create_task(db, creator=alice, title="Torque spec sheet", assignee=bob,
                                           settings=settings)
    await db.commit()
    await async_service.assign_task(db, task=task, assignee=carol, actor=alice, settings=settings)
    task.title = "Torque spec sheet v2"
    await async_service.task_changed(db, task=task, actor=alice, settings=settings)
    await db.commit()

    assert [(m.kind, m.recipient_user_id) for m in await rows(db)] == [
        (MessageKind.TASK_ASSIGNED, bob.id),
        (MessageKind.TASK_UNASSIGNED, bob.id),
        (MessageKind.TASK_ASSIGNED, carol.id),
        (MessageKind.TASK_UPDATED, carol.id),
    ]
    assert len(dispatched) == 4


async def test_get_db_commits_only_what_the_endpoint_commits(async_session_factory, users, dispatched, settings,
                                                             monkeypatch):
    monkeypatch.setattr(app.db, "get_async_session_factory", lambda: async_session_factory)
    api = FastAPI()

    @api.post("/announce")
    async def announce(commit: bool, db: AsyncSession = Depends(get_db)):
        author = await db.scalar(select(User).where(User.email == "alice@example.com"))
        ann = await async_service.post_announcement(db, author=author, title="t", body="b", settings=settings)
        if commit:
            await db.commit()
        return {"id": str(ann.id), "title": ann.title}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        assert (await client.post("/announce", params={"commit": False})).status_code == 200
        assert dispatched == []
        resp = await client.post("/announce", params={"commit": True})
        assert resp.json()["title"] == "t"  # readable after commit (expire_on_commit=False)

    async with async_session_factory() as s:
        [msg] = await rows(s)
    assert dispatched == [str(msg.id)]
