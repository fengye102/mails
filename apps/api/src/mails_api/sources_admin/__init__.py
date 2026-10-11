"""按来源 kind 解析管理适配器。

各来源实现放在本包下的独立模块里（cloudflare_temp_email.py / outlook_plus.py / cymail.py），
都提供 `build(client) -> SourceAdmin`，路由层只依赖本模块的 `admin_for`。
"""

import importlib

import httpx

from mails_api.sources_admin.base import SourceAdmin

ADMIN_MODULES = {
    "cloudflare_temp_email": "mails_api.sources_admin.cloudflare_temp_email",
    "outlook_plus": "mails_api.sources_admin.outlook_plus",
    "cymail": "mails_api.sources_admin.cymail",
}


def admin_for(kind: str, client: httpx.AsyncClient) -> SourceAdmin:
    module_name = ADMIN_MODULES.get(kind)
    if module_name is None:
        raise KeyError(kind)
    module = importlib.import_module(module_name)
    return module.build(client)
