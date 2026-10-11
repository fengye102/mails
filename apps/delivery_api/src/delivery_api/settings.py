from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DELIVERY_", extra="ignore")
    database_url: str = "sqlite+pysqlite:///:memory:"
    operator_token: str = ""
    encryption_key: str = ""
    # 取件链接的对外基址（例如 https://mail.orangeai.cfd）；留空则返回相对路径。
    pickup_base_url: str = ""
    pickup_ttl_seconds: int = 900
    session_ttl_seconds: int = 1800
    pickup_failure_limit: int = 5
    pickup_failure_window_seconds: int = 300
    pickup_backoff_seconds: int = 300
    cleanup_interval_seconds: int = 60
