"""Celery entry point: `celery -A app.worker.tasks:celery_app worker -Q discord` (and `beat`)."""

import math
import uuid
from functools import lru_cache

from celery.signals import worker_process_init

from app.celery_client import get_celery
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.discord import DiscordClient
from app.worker import sweeper
from app.worker.delivery import Deliverer

settings = get_settings()
celery_app = get_celery()


@worker_process_init.connect
def _fresh_connections_after_fork(**_):
    # Don't share pooled DB connections across prefork children.
    get_engine().dispose(close=False)


@lru_cache
def get_deliverer() -> Deliverer:
    client = DiscordClient(
        settings.discord_bot_token.get_secret_value(),
        base_url=settings.discord_api_base,
        user_agent_url=settings.discord_user_agent_url,
        timeout=settings.discord_timeout_seconds,
    )
    return Deliverer(get_session_factory(), client, settings)


@celery_app.task(name="discord.deliver", rate_limit=settings.deliver_rate_limit)
def deliver(outbox_id: str) -> None:
    outcome = get_deliverer().deliver(uuid.UUID(outbox_id))
    if outcome.retry_in is not None:
        deliver.apply_async(args=[outbox_id], countdown=max(1, math.ceil(outcome.retry_in)), queue="discord")


@celery_app.task(name="discord.sweep")
def sweep() -> None:
    sweeper.sweep(
        get_session_factory(),
        lambda outbox_id: deliver.apply_async(args=[outbox_id], queue="discord"),
        settings,
    )
