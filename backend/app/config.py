from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://pm:pm@localhost:5432/pm_dashboard"
    redis_url: str = "redis://localhost:6379/0"

    discord_bot_token: SecretStr = SecretStr("")
    discord_client_id: str = ""
    discord_client_secret: SecretStr = SecretStr("")
    discord_api_base: str = "https://discord.com/api/v10"
    discord_user_agent_url: str = "https://github.com/pm-dashboard"
    discord_timeout_seconds: float = 10.0

    frontend_base_url: str = "http://localhost:5173"
    frontend_origins: list[str] = ["http://localhost:5173"]

    use_webhooks: bool = True
    webhook_name: str = "PM Dashboard"

    # Delivery policy (see "Reliability" in the design doc).
    max_attempts: int = 5
    backoff_seconds: list[int] = [10, 30, 120, 600, 1800]
    # Delays up to this long are scheduled with a Celery countdown; longer ones wait for the sweeper.
    max_countdown_seconds: int = 60
    stuck_after_seconds: int = 300
    sweep_interval_seconds: int = 30
    sweep_grace_seconds: int = 60
    sweep_batch_size: int = 500
    deliver_rate_limit: str = "10/s"

    # Long-note handling. Summaries are design-only for now: no LLM is called.
    summary_threshold_chars: int = 1500


@lru_cache
def get_settings() -> Settings:
    return Settings()
