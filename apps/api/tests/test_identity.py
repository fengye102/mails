from datetime import datetime, timezone
from uuid import UUID

from mails_api.models import Mailbox, Message, ProviderConnection
from mails_api.services.identity import identity_statuses


def test_identity_status_marks_duplicate_valid_rfc_ids_without_merging(engine):
    from mails_api.models import Base
    from sqlalchemy.orm import Session

    Base.metadata.create_all(engine)
    with Session(engine) as db:
        connection = ProviderConnection(
            kind="outlook_plus",
            name="source",
            base_url="http://upstream",
            encrypted_secret=b"secret",
            status="active",
        )
        db.add(connection)
        db.flush()
        mailbox = Mailbox(
            connection_id=connection.id,
            provider_mailbox_key="box",
            address="box@example.com",
            sync_state="idle",
        )
        db.add(mailbox)
        db.flush()
        duplicate = "<same@example.com>"
        messages = [
            Message(
                id=UUID(int=1),
                mailbox_id=mailbox.id,
                provider_message_key="graph:1",
                rfc_message_id=duplicate,
                sender="a@example.com",
                recipient="box@example.com",
                subject="one",
                snippet="one",
                received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                body_text="one",
            ),
            Message(
                id=UUID(int=2),
                mailbox_id=mailbox.id,
                provider_message_key="imap:9",
                rfc_message_id=duplicate,
                sender="a@example.com",
                recipient="box@example.com",
                subject="one",
                snippet="one",
                received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                body_text="one",
            ),
            Message(
                id=UUID(int=3),
                mailbox_id=mailbox.id,
                provider_message_key="graph:3",
                rfc_message_id="not-an-rfc-id",
                sender="b@example.com",
                recipient="box@example.com",
                subject="two",
                snippet="two",
                received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                body_text="two",
            ),
        ]
        db.add_all(messages)
        db.commit()

        statuses = identity_statuses(db, messages)

    assert statuses[messages[0].id] == "same_rfc_id"
    assert statuses[messages[1].id] == "same_rfc_id"
    assert statuses[messages[2].id] == "unverified"

