"""Explicit provider registry; no dynamic plugin loading."""

import httpx
from mails_api.providers.base import MailProvider
from mails_api.providers.outlook_plus import OutlookPlusAdapter


def provider_for(kind: str, client: httpx.AsyncClient) -> MailProvider:
    if kind == "outlook_plus":
        return OutlookPlusAdapter(client)
    if kind == "cloudflare_temp_email":
        from mails_api.providers.cloudflare_temp_email import CloudflareTempEmailAdapter
        return CloudflareTempEmailAdapter(client)
    if kind == "cymail":
        from mails_api.providers.cymail import CYMailAdapter
        return CYMailAdapter(client)
    raise ValueError("UNSUPPORTED_PROVIDER_KIND")


PROVIDER_KINDS = ("outlook_plus", "cloudflare_temp_email", "cymail")
