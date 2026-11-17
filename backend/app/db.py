from collections.abc import AsyncIterator
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_session_factory(engine: Engine) -> sessionmaker:
    # expire_on_commit=False: the worker commits between claim, send and record,
    # and keeps reading the same objects across those commits.
    return sessionmaker(bind=engine, expire_on_commit=False)


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker:
    return make_session_factory(get_engine())


# --- async (API only; the Celery worker, beat and CLI use the sync engine above) -------

def make_async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: an async session can't lazy-load expired attributes, so objects
    # must stay readable after the endpoint commits (e.g. when building the response).
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@lru_cache
def get_async_engine() -> AsyncEngine:
    # postgresql+psycopg works for both: SQLAlchemy picks psycopg's async driver here.
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    return make_async_session_factory(get_async_engine())


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. The endpoint commits; anything it leaves
    uncommitted is rolled back when the request ends."""
    async with get_async_session_factory()() as session:
        yield session
