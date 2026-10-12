"""把引擎的别名快照对齐成 C 站库存：幂等 upsert + 幽灵行标记。

规则（对应设计规格不变量 6 与「库存是唯一真相来源」）：

1. **以上游快照为准建库存**：上游有、本地没有 → 新建（上游可用即 available）。
2. **上游停用**（`active=false`）→ 本地置 `inactive`：别名还在但收不到信，
   不能被分配出去（发出去等于让客户等一封永远不会来的邮件）。
3. **上游已不存在的本地行** → 置 `missing`，**绝不删除**：引擎看不到订单，
   「上游没了」不等于「可以回收」；删行会让 C 站的分配记录变成悬空外键。
4. **「上游已不存在但本地有有效分配」的行不动**，只计数上报：正在取件的人
   不能被一次同步打掉。人工看到这个计数再决定怎么处理。
5. **本地同地址出现多行**（模型没有唯一约束，历史手工登记可能撞车）→ 不猜，
   计数上报，相关行全部不动。
6. **运营显式停用（`disabled`）的行不被同步改写**：人的决定优先于同步。
7. 同步只动库存行的状态，不动邮件、不动分配、不动 token。

地址比较一律归一化（`strip().lower()`）——上游大小写变了不该变成两行库存。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from delivery_api.icloud_engine import AliasSnapshot
from delivery_api.models import DeliveryAllocation, InventoryMailbox

# 同步只动库存行状态，别的字段（邮件、分配、token）不归同步管。
DEFAULT_SOURCE_LABEL = "icloud-hme"


@dataclass
class SyncOutcome:
    """一次同步的账。计数只报事实，不含任何凭据。"""

    upstream_total: int = 0
    added: int = 0
    reactivated: int = 0  # missing/inactive → available（上游重新可用）
    deactivated: int = 0  # available → inactive（上游停用）
    missing: int = 0  # → missing（上游已不存在）
    protected: int = 0  # 上游已不存在但有有效分配：不动，只报
    ambiguous: int = 0  # 同地址多行：不猜
    disabled_kept: int = 0  # 运营停用：同步不碰

    def as_dict(self) -> dict:
        return asdict(self)


def _normalize(address: str) -> str:
    return (address or "").strip().lower()


def _has_active_allocation(db: Session, mailbox_id: str, now: datetime) -> bool:
    return (
        db.scalar(
            select(DeliveryAllocation.id)
            .where(
                DeliveryAllocation.mailbox_id == mailbox_id,
                DeliveryAllocation.status == "active",
                DeliveryAllocation.expires_at > now,
            )
            .limit(1)
        )
        is not None
    )


def apply_alias_snapshot(
    db: Session,
    snapshots: list[AliasSnapshot],
    *,
    source_label: str = DEFAULT_SOURCE_LABEL,
    now: datetime,
) -> SyncOutcome:
    """按上游快照对齐本地库存。**不提交事务**：由调用方决定何时 commit。"""
    outcome = SyncOutcome(upstream_total=len(snapshots))
    rows = db.scalars(
        select(InventoryMailbox).where(InventoryMailbox.source_label == source_label)
    ).all()
    by_address: dict[str, list[InventoryMailbox]] = {}
    for row in rows:
        by_address.setdefault(_normalize(row.address), []).append(row)

    for snapshot in snapshots:
        address = _normalize(snapshot.address)
        existing = by_address.get(address, [])
        if not existing:
            db.add(
                InventoryMailbox(
                    address=address,
                    source_label=source_label,
                    status="available" if snapshot.active else "inactive",
                )
            )
            outcome.added += 1
            continue
        if len(existing) > 1:
            outcome.ambiguous += 1
            continue
        row = existing[0]
        if row.status == "disabled":
            # 运营显式停用是人的决定：上游可用也不自动放开，只报出来。
            outcome.disabled_kept += 1
        elif snapshot.active:
            if row.status in ("missing", "inactive"):
                row.status = "available"
                outcome.reactivated += 1
        elif row.status in ("available", "missing"):
            row.status = "inactive"
            outcome.deactivated += 1

    upstream_addresses = {_normalize(snapshot.address) for snapshot in snapshots}
    for address, address_rows in by_address.items():
        if address in upstream_addresses:
            continue
        if len(address_rows) > 1:
            outcome.ambiguous += 1
            continue
        row = address_rows[0]
        if row.status == "disabled":
            outcome.disabled_kept += 1
        elif row.status in ("available", "inactive"):
            row.status = "missing"
            outcome.missing += 1
        elif row.status == "allocated":
            # 有有效分配的别名属于正在进行的交付，同步不碰；只报出来让运营看见。
            if _has_active_allocation(db, row.id, now):
                outcome.protected += 1
            else:
                row.status = "missing"
                outcome.missing += 1
    return outcome
