from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ICLOUD_", extra="ignore")
    database_url: str = "sqlite+pysqlite:///:memory:"
    operator_token: str = ""
    # base64 的 32 字节 AES-256-GCM 密钥；Cookie / App Password / Apple 会话都用它加密落库。
    data_key: str = ""
    apple_manage_base_url: str = "https://account.apple.com"
    apple_idmsa_base_url: str = "https://idmsa.apple.com/appleauth/auth"
    icloud_host: str = "icloud.com"
    upstream_proxy: str = ""
    # 建别名最小间隔（秒）：管理员选的节流下限，Apple 临时封禁会在此基础上取更晚的时间。
    alias_min_interval_seconds: int = 180
    alias_default_count: int = 1
    # 定时创建默认关闭，必须由界面显式打开。
    scheduler_default_enabled: bool = False
    browser_auth_ttl_seconds: int = 300
    inventory_sync_interval_seconds: int = 300
    pickup_refresh_interval_seconds: int = 15
    request_timeout_seconds: int = 30
