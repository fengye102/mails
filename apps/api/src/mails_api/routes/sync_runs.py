"""Owner-only sync trigger and sanitized run history."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from mails_api.db import get_db
from mails_api.models import Mailbox, ProviderConnection, SyncRun, utc_now
from mails_api.routes.connections import (
    allowed_origin,
    checked_time,
    encryption_key,
    get_http_client,
)
from mails_api.security.csrf import require_csrf, require_member, require_owner
from mails_api.services.sync import sync_mailbox

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_member)])


def run_payload(run: SyncRun) -> dict:
    return {
        "id": str(run.id),
        "connection_id": str(run.connection_id),
        "mailbox_id": str(run.mailbox_id),
        "started_at": checked_time(run.started_at),
        "finished_at": checked_time(run.finished_at),
        "status": run.status,
        "error_code": run.error_code,
        "error_summary": run.error_summary,
        "new_message_count": run.new_message_count,
    }


@router.post("/mailboxes/{mailbox_id}/sync", dependencies=[Depends(require_owner), Depends(require_csrf)])
async def trigger_sync(
    mailbox_id: UUID,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> dict:
    mailbox = db.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(status_code=404, detail="MAILBOX_NOT_FOUND")
    connection = db.get(ProviderConnection, mailbox.connection_id)
    allowed_origin(connection.base_url, request)
    from mails_api.routes.connections import get_adapter_for
    adapter = get_adapter_for(connection.kind, client)
    run = await sync_mailbox(
        db, mailbox_id, adapter, utc_now(), encryption_key=encryption_key(request)
    )
    return run_payload(run)


@router.get("/sync-runs")
def list_sync_runs(
    db: Annotated[Session, Depends(get_db)],
    mailbox_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=100),
) -> list[dict]:
    statement = select(SyncRun).order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
    if mailbox_id is not None:
        statement = statement.where(SyncRun.mailbox_id == mailbox_id)
    statement = statement.limit(limit)
    return [run_payload(run) for run in db.scalars(statement)]
