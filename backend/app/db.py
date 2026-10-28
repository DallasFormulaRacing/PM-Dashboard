from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
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
