import os

import pytest
import respx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, make_session_factory
from app.discord import DiscordClient
from app.messaging import outbox
from app.models import DiscordChannel, User
from app.worker.delivery import Deliverer

DISCORD = "https://discord.test/api"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        frontend_base_url="https://app.example.com",
        discord_api_base=DISCORD,
        backoff_seconds=[10, 30, 120, 600, 1800],
        summary_threshold_chars=200,
    )


@pytest.fixture
def engine():
    # Set TEST_DATABASE_URL to run against Postgres (needed to exercise SKIP LOCKED and JSONB).
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        eng = create_engine(url)
        Base.metadata.drop_all(eng)
    else:
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture
def dispatched():
    ids: list[str] = []
    outbox.set_dispatcher(ids.append)
    yield ids
    outbox.set_dispatcher(None)


@pytest.fixture
def discord():
    with respx.mock(base_url=DISCORD, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def client():
    c = DiscordClient("test-token", base_url=DISCORD)
    yield c
    c.close()


@pytest.fixture
def deliverer(session_factory, client, settings) -> Deliverer:
    return Deliverer(session_factory, client, settings)


@pytest.fixture
def people(session):
    alice = User(name="Alice Lead", email="alice@example.com", avatar_url="https://cdn.example.com/a.png",
                 discord_user_id="111")
    bob = User(name="Bob", email="bob@example.com", discord_user_id="222")
    carol = User(name="Carol", email="carol@example.com", discord_user_id="333")
    unlinked = User(name="Dana", email="dana@example.com")
    session.add_all([alice, bob, carol, unlinked])
    session.add_all([
        DiscordChannel(purpose="announcements", guild_id="900", channel_id="500"),
        DiscordChannel(purpose="assignments_fallback", guild_id="900", channel_id="600"),
    ])
    session.commit()
    return {"alice": alice, "bob": bob, "carol": carol, "unlinked": unlinked}
