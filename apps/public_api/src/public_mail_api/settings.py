"""Configuration isolated from the private A service."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PUBLIC_MAIL_", extra="ignore")

    database_url: str = "sqlite+pysqlite:///:memory:"
    upstream_url: str = "https://worker.example"
    admin_key: str = ""
    encryption_key: str = ""
    allowed_origins: list[str] = []
    cookie_secure: bool = True
    ttl_seconds: int = 86400
    capability_cookie_name: str = "public_mail_capability"
    turnstile_secret: str | None = None
