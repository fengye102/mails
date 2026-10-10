from delivery_api.models import Base, InventoryMailbox, PickupCredential


def test_delivery_models_have_expected_tables():
    assert {"inventory_mailboxes", "delivery_allocations", "pickup_credentials", "delivery_audit_events"}.issubset(Base.metadata.tables)
    assert "status" in InventoryMailbox.__table__.c
    assert "consumed_at" in PickupCredential.__table__.c
