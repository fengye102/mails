from delivery_api.models import (
    Base,
    DeliveryMessage,
    InventoryMailbox,
    PickupCredential,
    PickupSession,
)


def test_delivery_models_have_expected_tables():
    assert {
        "inventory_mailboxes",
        "delivery_allocations",
        "pickup_credentials",
        "pickup_attempts",
        "pickup_sessions",
        "delivery_messages",
        "delivery_audit_events",
    }.issubset(Base.metadata.tables)
    assert "status" in InventoryMailbox.__table__.c
    assert "consumed_at" in PickupCredential.__table__.c


def test_mailbox_no_longer_stores_messages_inline():
    """邮件只在 delivery_messages 里有一份真相，邮箱行上不再存 JSON。"""
    assert "messages" not in InventoryMailbox.__table__.c
    assert {"mailbox_id", "provider_message_id", "body_text", "otp_code", "link"}.issubset(
        DeliveryMessage.__table__.c.keys()
    )
    unique = {
        tuple(sorted(column.name for column in constraint.columns))
        for constraint in DeliveryMessage.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("mailbox_id", "provider_message_id") in unique


def test_pickup_credential_supports_code_and_key_modes():
    columns = PickupCredential.__table__.c
    assert "code_hash" in columns and "key_hash" in columns
    # 会话独立成表，同一凭证可以并存多个会话
    assert {"credential_id", "allocation_id", "token_hash", "expires_at"}.issubset(
        PickupSession.__table__.c.keys()
    )
