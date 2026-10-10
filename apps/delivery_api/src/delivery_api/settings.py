from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DELIVERY_", extra="ignore")
    database_url: str = "sqlite+pysqlite:///:memory:"
    operator_token: str = ""
    encryption_key: str = ""
    pickup_ttl_seconds: int = 900
    session_ttl_seconds: int = 1800
