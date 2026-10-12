"""把引擎本地的别名库存与上游快照对齐。

规则（对应设计规格 `2026-10-12-phase-d-icloud-engine-design.md` 安全与行为不变量 6）：

- 上游有、本地没有 → 新增；
- 上游有、本地有 → 更新 label / anonymous_id，状态按上游 active 翻正；
- 上游没有、本地有 → 标记 `state="missing"`，**不删除**。

为什么标记而不删：「上游没了」不等于「可以回收」。引擎看不到 C 站的分配关系，
有有效分配的别名必须留着（不变量 6），删掉会让 C 站的库存行凭空消失、客户取件断链。
真正的删除只发生在两条明确路径：用户主动删别名（DELETE 路由，上游成功后才清本地行），
以及运维显式清理。

调用方必须传**完整**的上游快照：只看一页会把「不在本页」误判成「上游已删」。
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from icloud_api.aliases import Alias
from icloud_api.models import ICloudAlias, utc_now

# 上游已不存在的本地行标记成这个状态；不是上游的状态，是本地的同步标记。
MISSING_STATE = "missing"


@dataclass(frozen=True)
class AliasSyncOutcome:
    """一次快照对齐的结果计数。"""

    added: int
    updated: int
    missing: int

    def as_dict(self) -> dict:
        return {"added": self.added, "updated": self.updated, "missing": self.missing}


def apply_alias_snapshot(
    db: Session,
    *,
    account_id: str,
    upstream: list[Alias],
    local: list[ICloudAlias] | None = None,
    now: datetime | None = None,
) -> AliasSyncOutcome:
    """把上游快照落进本地库存，并标出上游已消失的行。

    `local` 只是给测试和批量调用省一次查询用的；不传就现查。
    函数只改内存对象，提交由调用方负责（和本仓库其余写路径一致）。
    """
    moment = now or utc_now()
    if local is None:
        local = list(
            db.scalars(select(ICloudAlias).where(ICloudAlias.account_id == account_id))
        )

    # 地址大小写不敏感比对：上游可能改大小写，按原样比对会凭空多出一行幽灵行。
    by_address = {row.address.strip().lower(): row for row in local}
    seen: set[str] = set()
    added = 0
    updated = 0

    for alias in upstream:
        address = alias.email.strip()
        if not address:
            continue
        key = address.lower()
        seen.add(key)
        want_state = "active" if alias.active else "inactive"
        row = by_address.get(key)
        if row is None:
            db.add(
                ICloudAlias(
                    account_id=account_id,
                    address=address,
                    anonymous_id=alias.anonymous_id,
                    label=alias.label,
                    state=want_state,
                    synced_at=moment,
                )
            )
            added += 1
            continue

        changed = False
        if row.anonymous_id != alias.anonymous_id:
            row.anonymous_id = alias.anonymous_id
            changed = True
        if row.label != alias.label:
            row.label = alias.label
            changed = True
        if row.state != want_state:
            row.state = want_state
            changed = True
        row.synced_at = moment
        if changed:
            updated += 1

    missing = 0
    for row in local:
        if row.address.strip().lower() in seen:
            continue
        missing += 1
        if row.state != MISSING_STATE:
            row.state = MISSING_STATE
            row.synced_at = moment

    return AliasSyncOutcome(added=added, updated=updated, missing=missing)
