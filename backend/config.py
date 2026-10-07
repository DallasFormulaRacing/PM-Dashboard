from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    discord_client_id: str
    discord_client_secret: str
    discord_redirect_uri: str
    discord_bot_token: str
    discord_guild_id: str
    database_url: str
    jwt_secret: str


settings = Settings()