"""Explicit provider registry; no dynamic plugin loading."""

import httpx

from mails_api.providers.base import MailProvider
from mails_api.providers.outlook_plus import OutlookPlusAdapter

# 需要「来源账号 + 别名」寻址的来源：连接的 config 必须带 account_id，
# 地址要编进 provider mailbox key（上游按「账号 + 别名」定位邮箱，裸地址不够）。
# 其余来源直接用裸地址寻址，登记时地址本身就是 provider key。
ACCOUNT_SCOPED_KINDS = ("cymail", "icloud_hme")

_ACCOUNT_ID_REQUIRED_CODE = {
    "cymail": "CYMAIL_ACCOUNT_ID_REQUIRED",
    "icloud_hme": "ICLOUD_HME_ACCOUNT_ID_REQUIRED",
}


class AccountIdRequired(Exception):
    """连接配置缺 account_id。`code` 是固定错误码，由路由层映射成 422。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def account_id_required_code(kind: str) -> str:
    """连接缺 account_id 时该来源上报的固定错误码。"""
    return _ACCOUNT_ID_REQUIRED_CODE[kind]


def mailbox_key_for(kind: str, config: dict | None, address: str) -> str:
    """地址 → provider mailbox key；不需要账号寻址的来源返回规范化后的裸地址。

    登记、实时读信、定时同步三处都走这一个入口：寻址规则只能有一份，
    否则「登记时按 A 存、同步时按 B 读」这类不一致会退化成读不到信。
    """
    value = address.strip().lower()
    if kind not in ACCOUNT_SCOPED_KINDS:
        return value
    account_id = (config or {}).get("account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        raise AccountIdRequired(account_id_required_code(kind))
    if kind == "cymail":
        from mails_api.providers.cymail import mailbox_key as cymail_mailbox_key

        return cymail_mailbox_key(account_id, value)
    from mails_api.providers.icloud_hme import mailbox_key as icloud_hme_mailbox_key

    return icloud_hme_mailbox_key(account_id, value)


def provider_for(kind: str, client: httpx.AsyncClient) -> MailProvider:
    if kind == "outlook_plus":
        return OutlookPlusAdapter(client)
    if kind == "cloudflare_temp_email":
        from mails_api.providers.cloudflare_temp_email import CloudflareTempEmailAdapter
        return CloudflareTempEmailAdapter(client)
    if kind == "cymail":
        from mails_api.providers.cymail import CYMailAdapter
        return CYMailAdapter(client)
    if kind == "icloud_hme":
        from mails_api.providers.icloud_hme import ICloudHMEAdapter
        return ICloudHMEAdapter(client)
    raise ValueError("UNSUPPORTED_PROVIDER_KIND")


PROVIDER_KINDS = ("outlook_plus", "cloudflare_temp_email", "cymail", "icloud_hme")
