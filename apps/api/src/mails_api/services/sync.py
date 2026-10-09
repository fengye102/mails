"""Single-worker, per-engine/per-mailbox non-queuing synchronization."""

import asyncio
from datetime import timedelta, timezone
from weakref import WeakKeyDictionary

import httpx
from cryptography.exceptions import InvalidTag
from mails_api.models import Mailbox, Message, ProviderConnection, SyncRun, utc_now
from mails_api.providers.base import ProviderError
from mails_api.providers.outlook_plus import OutlookPlusAdapter
from mails_api.security.crypto import decrypt_secret
from mails_api.services.extract import extract_code
from sqlalchemy import select

# Entries exist only while running; Engine weak keys avoid retaining disposed apps.
_running = WeakKeyDictionary()
_SAFE_ERRORS = {
    code: ProviderError(code).summary
    for code in (
        "UPSTREAM_AUTH",
        "UPSTREAM_TIMEOUT",
        "UPSTREAM_CONTRACT",
        "UPSTREAM_UNAVAILABLE",
    )
}
_SAFE_ERRORS.update(
    {
        "SYNC_CANCELLED": "Synchronization was cancelled.",
        "SYNC_FAILED": "Synchronization failed.",
        "CONNECTION_SECRET_INVALID": "Connection secret is unavailable.",
    }
)


def _safe_code(exc):
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "UPSTREAM_TIMEOUT"
    if isinstance(exc, ProviderError) and exc.code in _SAFE_ERRORS:
        return exc.code
    return "UPSTREAM_CONTRACT"


def _record_error(run, code):
    code = code if code in _SAFE_ERRORS else "UPSTREAM_CONTRACT"
    # Accumulate distinct safe issue kinds, never arbitrary exception strings.
    # error_code has a 64-character database limit; primary error stays stable.
    run.error_code = run.error_code or code
    summaries = set((run.error_summary or "").split(" | ")) - {""}
    summaries.add(_SAFE_ERRORS[code])
    run.error_summary = " | ".join(sorted(summaries))


async def sync_mailbox(
    db, mailbox_id, adapter, now, encryption_key: bytes | None = None
):
    engine = db.get_bind()
    active = _running.setdefault(engine, {})
    if mailbox_id in active:
        return db.get(SyncRun, active[mailbox_id][1])
    mailbox = db.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise LookupError("MAILBOX_NOT_FOUND")
    connection = db.get(ProviderConnection, mailbox.connection_id)
    lock = asyncio.Lock()
    async with lock:
        run = SyncRun(
            connection_id=mailbox.connection_id,
            mailbox_id=mailbox.id,
            started_at=now,
            status="running",
            new_message_count=0,
        )
        db.add(run)
        mailbox.sync_state = "running"
        db.commit()
        active[mailbox_id] = (lock, run.id)
        first_sync = mailbox.last_synced_at is None
        try:
            if connection.status != "active":
                raise ProviderError("UPSTREAM_UNAVAILABLE")
            try:
                if encryption_key is None:
                    if isinstance(adapter, OutlookPlusAdapter):
                        raise ValueError
                    secret = ""
                else:
                    secret = decrypt_secret(encryption_key, connection.encrypted_secret)
            except (ValueError, TypeError, InvalidTag, UnicodeError):
                _record_error(run, "CONNECTION_SECRET_INVALID")
                raise ProviderError("UPSTREAM_UNAVAILABLE") from None
            result = await adapter.list_messages(
                connection.base_url, secret, mailbox.address
            )
            for issue in result.issues:
                _record_error(run, issue.code)
            summaries = result.summaries[:100]
            keys = [item.provider_message_key for item in summaries]
            existing = set(
                db.scalars(
                    select(Message.provider_message_key).where(
                        Message.mailbox_id == mailbox.id,
                        Message.provider_message_key.in_(keys),
                    )
                )
            )
            for summary in summaries:
                key = summary.provider_message_key
                received = summary.received_at.replace(
                    tzinfo=summary.received_at.tzinfo or timezone.utc
                )
                if key in existing or (
                    first_sync and received < now - timedelta(days=7)
                ):
                    continue
                try:
                    detail = await adapter.get_message(
                        connection.base_url, secret, mailbox.address, summary
                    )
                    if detail.provider_message_key != key:
                        raise ProviderError("UPSTREAM_CONTRACT")
                    message = Message(
                        mailbox_id=mailbox.id,
                        provider_message_key=key,
                        rfc_message_id=detail.rfc_message_id,
                        sender=detail.sender,
                        recipient=detail.recipient,
                        subject=detail.subject,
                        snippet=detail.snippet,
                        received_at=detail.received_at,
                        body_text=detail.body_text,
                        verification_code=extract_code(
                            detail.subject, detail.body_text
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - isolate malformed provider data safely
                    _record_error(run, _safe_code(exc))
                    continue
                db.add(message)
                existing.add(key)
                run.new_message_count += 1
            run.status = "partial" if run.error_code else "success"
            mailbox.last_synced_at = now
        except asyncio.CancelledError:
            run.status = "failed"
            _record_error(run, "SYNC_CANCELLED")
            raise
        except Exception as exc:  # noqa: BLE001 - isolate malformed provider data safely
            run.status = "failed"
            _record_error(run, _safe_code(exc))
        finally:
            try:
                run.finished_at = utc_now()
                mailbox.sync_state = run.status
                db.commit()
            finally:
                active.pop(mailbox_id, None)
        return run


async def sync_all_mailboxes(app):
    """Independent sessions/tasks ensure a slow mailbox cannot block its peers."""
    from mails_api.routes.connections import allowed_origin
    from mails_api.security.crypto import decode_key
    from sqlalchemy.orm import Session
    from starlette.requests import Request

    with Session(app.state.engine) as db:
        ids = list(
            db.scalars(
                select(Mailbox.id)
                .join(ProviderConnection)
                .where(ProviderConnection.status == "active")
            )
        )
    key = decode_key(app.state.settings.encryption_key)

    async def sync_one(mailbox_id, adapter):
        with Session(app.state.engine) as db:
            mailbox = db.get(Mailbox, mailbox_id)
            connection = db.get(ProviderConnection, mailbox.connection_id)
            allowed_origin(connection.base_url, Request({"type": "http", "app": app}))
            return await sync_mailbox(
                db, mailbox_id, adapter, utc_now(), encryption_key=key
            )

    async def run(client):
        # Exceptions are isolated at the mailbox boundary, not leaked to logs.
        await asyncio.gather(
            *(sync_one(item, OutlookPlusAdapter(client)) for item in ids),
            return_exceptions=True,
        )

    if app.state.upstream_client is not None:
        await run(app.state.upstream_client)
    else:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            await run(client)
