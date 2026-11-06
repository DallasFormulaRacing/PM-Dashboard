import ssl
from functools import lru_cache

from celery import Celery

from app.config import Settings, get_settings


def make_celery(settings: Settings) -> Celery:
    app = Celery("pm_dashboard", broker=settings.redis_url)
    app.conf.update(
        task_ignore_result=True,  # delivery status lives in outbound_messages
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_default_queue="discord",
        broker_connection_retry_on_startup=True,
        broker_pool_limit=2,
        broker_transport_options={"visibility_timeout": 600},
        beat_schedule={
            "discord-sweep": {
                "task": "discord.sweep",
                "schedule": float(settings.sweep_interval_seconds),
                "options": {"queue": "discord", "expires": settings.sweep_interval_seconds},
            },
        },
    )
    if settings.redis_url.startswith("rediss://"):
        app.conf.broker_use_ssl = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    return app


@lru_cache
def get_celery() -> Celery:
    return make_celery(get_settings())
