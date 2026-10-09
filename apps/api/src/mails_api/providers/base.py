"""Provider DTOs and sanitized errors; no persistence dependencies."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class MessageSummary:
    provider_id: str
    method: str
    sender: str
    subject: str
    snippet: str
    received_at: datetime

    @property
    def provider_message_key(self) -> str:
        return f"{self.method}:{self.provider_id}"


@dataclass(frozen=True)
class MessageListIssue:
    absolute_index: int
    code: str


@dataclass(frozen=True)
class MessageListResult:
    summaries: list[MessageSummary]
    issues: list[MessageListIssue]


@dataclass(frozen=True)
class NormalizedMessage(MessageSummary):
    recipient: str
    body_text: str
    rfc_message_id: str | None


class ProviderError(Exception):
    """Only fixed, credential-free summaries may cross the provider boundary."""

    def __init__(self, code: str):
        self.code = code
        self.summary = {
            "UPSTREAM_UNAVAILABLE": "Upstream service is unhealthy.",
            "UPSTREAM_AUTH": "Upstream authentication failed.",
            "UPSTREAM_TIMEOUT": "Upstream request timed out.",
            "UPSTREAM_CONTRACT": "Upstream response does not meet the read contract.",
        }[code]
        super().__init__(self.summary)


class MailProvider(Protocol):
    """Read-only provider contract consumed by the unified sync service."""

    async def probe(self, base_url: str, secret: str, config: dict[str, Any] | None = None) -> None: ...

    async def check_mailbox(
        self, base_url: str, secret: str, address: str, config: dict[str, Any] | None = None
    ) -> str | None: ...

    async def list_messages(
        self, base_url: str, secret: str, address: str, config: dict[str, Any] | None = None
    ) -> MessageListResult: ...

    async def get_message(
        self,
        base_url: str,
        secret: str,
        address: str,
        summary: MessageSummary,
        config: dict[str, Any] | None = None,
    ) -> NormalizedMessage: ...
