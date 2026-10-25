"""Environment-based application configuration."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from app.connectors.notion_properties import NotionSourcePolicy


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or the root .env."""

    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")
    postgres_db: str = Field(default="f1rag_local", validation_alias="POSTGRES_DB")
    postgres_user: str = Field(default="f1rag_app", validation_alias="POSTGRES_USER")
    postgres_password: str = Field(default="", validation_alias="POSTGRES_PASSWORD")
    postgres_host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5433, validation_alias="POSTGRES_PORT")
    notion_api_token: SecretStr | None = Field(
        default=None,
        validation_alias="NOTION_API_TOKEN",
    )
    notion_api_version: str = Field(
        default="2026-03-11",
        validation_alias="NOTION_API_VERSION",
    )
    notion_allowed_page_ids: list[str] = Field(
        default_factory=list,
        validation_alias="NOTION_ALLOWED_PAGE_IDS",
    )
    notion_allowed_data_source_ids: list[str] = Field(
        default_factory=list,
        validation_alias="NOTION_ALLOWED_DATA_SOURCE_IDS",
    )
    notion_source_policies: dict[str, NotionSourcePolicy] = Field(
        default_factory=dict,
        validation_alias="NOTION_SOURCE_POLICIES",
    )

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def sqlalchemy_database_url(self) -> str:
        """Return a Psycopg SQLAlchemy URL without manual URL escaping."""

        if self.database_url:
            return self.database_url

        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    """Return one settings object per process."""

    return Settings()
