"""Environment-based application configuration."""

from functools import lru_cache
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode
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

    box_auth_mode: Literal["developer_token", "oauth"] | None = Field(
        default=None,
        validation_alias="BOX_AUTH_MODE",
    )
    box_account_label: str | None = Field(default=None, validation_alias="BOX_ACCOUNT_LABEL")
    box_developer_token: SecretStr | None = Field(
        default=None,
        validation_alias="BOX_DEVELOPER_TOKEN",
    )
    box_client_id: str | None = Field(default=None, validation_alias="BOX_CLIENT_ID")
    box_client_secret: SecretStr | None = Field(
        default=None,
        validation_alias="BOX_CLIENT_SECRET",
    )
    box_oauth_redirect_uri: str = Field(
        default="http://localhost:8765/callback",
        validation_alias="BOX_OAUTH_REDIRECT_URI",
    )
    box_oauth_token_path: Path = Field(
        default=REPOSITORY_ROOT / "backend" / ".local" / "box_oauth_tokens.json",
        validation_alias="BOX_OAUTH_TOKEN_PATH",
    )
    # Comma-separated in .env (e.g. 123,456); JSON lists are also accepted.
    box_allowed_file_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias="BOX_ALLOWED_FILE_IDS",
    )
    box_allowed_extensions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["pdf", "docx", "md", "txt"],
        validation_alias="BOX_ALLOWED_EXTENSIONS",
    )
    box_max_file_bytes: int = Field(
        default=25 * 1024 * 1024,
        gt=0,
        validation_alias="BOX_MAX_FILE_BYTES",
    )
    box_request_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        validation_alias="BOX_REQUEST_TIMEOUT_SECONDS",
    )

    @field_validator("box_allowed_file_ids", "box_allowed_extensions", mode="before")
    @classmethod
    def split_comma_list(cls, value: object) -> object:
        """Accept `a,b,c` or a JSON list; blank entries are dropped."""

        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

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
