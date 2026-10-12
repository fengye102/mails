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
    # iCloud 引擎（库存来源）：机器接口地址 + 运营 token + 要同步的账号。
    # 三者留空即「该来源未配置」，同步接口会明确拒绝而不是假装成功。
    icloud_engine_url: str = ""
    icloud_engine_token: str = ""
    icloud_engine_account_id: str = ""
    icloud_engine_source_label: str = "icloud-hme"
    # 取件读时拉上游邮件的最小间隔（同一邮箱）。<=0 关闭取件时刷新，只读本地归档。
    pickup_refresh_min_interval_seconds: int = 15
    # 邮箱池（对外供注册机）：租约、冷却、限流。数量级照 OEP 的默认值。
    pool_enabled: bool = True
    pool_default_lease_seconds: int = 600
    pool_max_lease_seconds: int = 3600
    pool_cooldown_seconds: int = 86400
    pool_default_rate_limit_per_minute: int = 60
    # 池可发的来源标签（逗号分隔）；留空表示用 pool.READABLE_SOURCE_LABELS 的内置清单。
    pool_source_labels: str = ""
