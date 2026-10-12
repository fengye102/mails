"""本地库存（`icloud_aliases`）与上游快照的对齐规则。

重点守设计规格不变量 6：**上游已消失的行只标记 missing，绝不删**。
引擎看不到 C 站的分配关系，「上游没了」不等于「可以回收」；
删掉会让 C 站库存行凭空消失、客户取件断链。

另一条容易漏的：地址比对必须大小写不敏感。上游改个大小写就按原样比对，
会凭空多出一行幽灵行，C 站那边就会把一个好邮箱标成不可用。
"""

from sqlalchemy.orm import Session

from icloud_api.alias_inventory import MISSING_STATE, apply_alias_snapshot
from icloud_api.aliases import Alias
from icloud_api.models import ICloudAccount, ICloudAlias


def _account(session: Session) -> ICloudAccount:
    row = ICloudAccount(name="主号", real_email="user@example.com", host="icloud.com")
    session.add(row)
    session.flush()
    return row


def _local(session: Session, account_id: str, address: str, *, anon="anon-1", state="active"):
    row = ICloudAlias(
        account_id=account_id, address=address, anonymous_id=anon, label="", state=state
    )
    session.add(row)
    session.flush()
    return row


def _upstream(address: str, *, anon="anon-1", label="", active=True) -> Alias:
    return Alias(email=address, anonymous_id=anon, label=label, active=active)


def test_adds_rows_that_exist_upstream_only(engine) -> None:
    with Session(engine) as session:
        account = _account(session)
        outcome = apply_alias_snapshot(
            session,
            account_id=account.id,
            upstream=[_upstream("new@icloud.com", anon="anon-new", label="新")],
        )
        session.commit()

        assert outcome.as_dict() == {"added": 1, "updated": 0, "missing": 0}
        row = session.query(ICloudAlias).one()
        assert row.address == "new@icloud.com"
        assert row.anonymous_id == "anon-new"
        assert row.label == "新"
        assert row.state == "active"


def test_ghost_row_is_marked_missing_and_kept(engine) -> None:
    """上游没了 → 只标记，绝不删（不变量 6）。"""
    with Session(engine) as session:
        account = _account(session)
        _local(session, account.id, "gone@icloud.com", anon="anon-gone")
        came_back = apply_alias_snapshot(session, account_id=account.id, upstream=[])
        session.commit()

        assert came_back.as_dict() == {"added": 0, "updated": 0, "missing": 1}
        rows = session.query(ICloudAlias).all()
        assert len(rows) == 1, "幽灵行被删了——有有效分配的别名会跟着消失"
        assert rows[0].state == MISSING_STATE


def test_address_comparison_is_case_insensitive(engine) -> None:
    """上游改大小写不能凭空造出幽灵行。"""
    with Session(engine) as session:
        account = _account(session)
        _local(session, account.id, "Mixed@iCloud.com", anon="anon-1")
        outcome = apply_alias_snapshot(
            session,
            account_id=account.id,
            upstream=[_upstream("mixed@icloud.com", anon="anon-1")],
        )
        session.commit()

        assert outcome.missing == 0, "大小写不同被当成了另一行"
        assert session.query(ICloudAlias).count() == 1


def test_inactive_upstream_flips_local_state(engine) -> None:
    with Session(engine) as session:
        account = _account(session)
        _local(session, account.id, "a@icloud.com", anon="anon-1", state="active")
        outcome = apply_alias_snapshot(
            session,
            account_id=account.id,
            upstream=[_upstream("a@icloud.com", anon="anon-1", label="改过", active=False)],
        )
        session.commit()

        assert outcome.updated == 1
        row = session.query(ICloudAlias).one()
        assert row.state == "inactive"
        assert row.label == "改过"


def test_missing_row_recovers_when_upstream_returns(engine) -> None:
    """标记是可逆的：别名回到上游就应恢复 active，不能卡在 missing。"""
    with Session(engine) as session:
        account = _account(session)
        _local(session, account.id, "back@icloud.com", anon="anon-1", state=MISSING_STATE)
        outcome = apply_alias_snapshot(
            session,
            account_id=account.id,
            upstream=[_upstream("back@icloud.com", anon="anon-1")],
        )
        session.commit()

        assert outcome.updated == 1
        assert session.query(ICloudAlias).one().state == "active"


def test_unchanged_row_is_not_counted_as_updated(engine) -> None:
    """重复同步必须幂等：内容没变就不算 updated，否则每轮同步都在写库。"""
    with Session(engine) as session:
        account = _account(session)
        _local(session, account.id, "same@icloud.com", anon="anon-1")
        outcome = apply_alias_snapshot(
            session,
            account_id=account.id,
            upstream=[_upstream("same@icloud.com", anon="anon-1")],
        )
        session.commit()

        assert outcome.as_dict() == {"added": 0, "updated": 0, "missing": 0}


def test_snapshot_is_scoped_to_one_account(engine) -> None:
    """另一个账号的别名不能被当成幽灵行。"""
    with Session(engine) as session:
        first = _account(session)
        second = _account(session)
        _local(session, second.id, "other@icloud.com", anon="anon-other")
        outcome = apply_alias_snapshot(session, account_id=first.id, upstream=[])
        session.commit()

        assert outcome.missing == 0
        assert session.query(ICloudAlias).filter(ICloudAlias.account_id == second.id).count() == 1
