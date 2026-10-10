"""Required deployment configuration, read from MAILS_* environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAILS_")

    database_url: str
    encryption_key: str
    public_origin: str
    upstream_origins: list[str]
