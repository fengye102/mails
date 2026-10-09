"""Provider DTOs and sanitized errors; no persistence dependencies."""

from dataclasses import dataclass
from datetime import datetime


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
