from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from delivery_api.models import (
    DeliveryAllocation,
    DeliveryAuditEvent,
    InventoryMailbox,
    utc_now,
)
from delivery_api.time_utils import as_utc


def cleanup_expired_allocations(db: Session, now: datetime | None = None) -> int:
    """Expire active allocations and release mailboxes without active leases."""
    current = now or utc_now()
    allocations = db.scalars(
        select(DeliveryAllocation)
        .where(DeliveryAllocation.status == "active")
        .with_for_update()
    ).all()
    expired = [
        allocation
        for allocation in allocations
        if as_utc(allocation.expires_at) <= current
    ]
    for allocation in expired:
        allocation.status = "expired"
        db.add(
            DeliveryAuditEvent(
                event_type="allocation_expired",
                allocation_id=allocation.id,
                mailbox_id=allocation.mailbox_id,
            )
        )
    for mailbox_id in {allocation.mailbox_id for allocation in expired}:
        mailbox = db.get(InventoryMailbox, mailbox_id)
        if mailbox is None or mailbox.status != "allocated":
            continue
        active_exists = db.scalar(
            select(DeliveryAllocation.id).where(
                DeliveryAllocation.mailbox_id == mailbox_id,
                DeliveryAllocation.status == "active",
            )
        )
        if active_exists is None:
            mailbox.status = "available"
    db.commit()
    return len(expired)
