"""取件凭证签发：一次性取件码（code）与可重复取件密钥（key）。

两种凭证共用同一套存储与校验语义：
- 只存 SHA-256 摘要，明文只在签发响应中出现一次；
- 都受分配有效期约束，重签会把旧凭证立刻置为过期；
- `key` 模式额外给出取件链接与一行发货文本，密钥放在 URL fragment 里。
"""

from datetime import datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from delivery_api.models import (
    DeliveryAllocation,
    DeliveryAuditEvent,
    PickupCredential,
)
from delivery_api.security import hash_secret, new_code, new_pickup_key
from delivery_api.time_utils import as_utc

MAX_CREDENTIAL_FAILURES = 5
MODES = ("code", "key")


def pickup_url(pickup_base_url: str, address: str, key: str) -> str:
    base = (pickup_base_url or "").rstrip("/")
    # 密钥放在 fragment：浏览器不会把它发给服务端，访问日志里也不会出现明文密钥。
    return f"{base}/pickup#{urlencode({'email': address, 'key': key})}"


def delivery_text(address: str, key: str, url: str) -> str:
    """一行发货文本，运营直接粘贴给客户（与 CYMail 的发货格式一致）。"""
    return f"{address}---{key}---{url}"


def invalidate_credentials(db: Session, allocation_id: str, now: datetime) -> int:
    """重签：把该分配下尚未使用的凭证立刻置为过期，旧码/旧密钥即时失效。"""
    credentials = db.scalars(
        select(PickupCredential).where(
            PickupCredential.allocation_id == allocation_id,
            PickupCredential.consumed_at.is_(None),
        )
    ).all()
    invalidated = 0
    for credential in credentials:
        if as_utc(credential.expires_at) > now:
            credential.expires_at = now
            invalidated += 1
    return invalidated


def issue_credential(
    db: Session,
    allocation: DeliveryAllocation,
    address: str,
    *,
    mode: str,
    ttl_seconds: int,
    now: datetime,
    pickup_base_url: str = "",
    reissue: bool = False,
) -> dict[str, object]:
    """签发一张取件凭证并提交；返回体里只有这一次能拿到明文。"""
    if reissue:
        invalidate_credentials(db, allocation.id, now)
    expires_at = min(
        as_utc(allocation.expires_at), now + timedelta(seconds=max(1, ttl_seconds))
    )
    plaintext = new_pickup_key() if mode == "key" else new_code()
    credential = PickupCredential(
        allocation_id=allocation.id,
        code_hash=None if mode == "key" else hash_secret(plaintext),
        key_hash=hash_secret(plaintext) if mode == "key" else None,
        expires_at=expires_at,
    )
    db.add(credential)
    db.add(
        DeliveryAuditEvent(
            event_type="pickup_credential_created",
            allocation_id=allocation.id,
            mailbox_id=allocation.mailbox_id,
            details={"mode": mode, "reissue": reissue},
        )
    )
    db.commit()

    body: dict[str, object] = {
        "pickup_id": credential.id,
        "mode": mode,
        "pickup_code" if mode == "code" else "pickup_key": plaintext,
        "expires_at": credential.expires_at.isoformat(),
    }
    if mode == "key":
        url = pickup_url(pickup_base_url, address, plaintext)
        body["pickup_url"] = url
        body["delivery_text"] = delivery_text(address, plaintext, url)
    return body
