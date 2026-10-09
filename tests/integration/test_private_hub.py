from pathlib import Path

import pytest


def test_deployment_contract():
    root = Path(__file__).resolve().parents[2]
    compose = (root / "infra/compose.yaml").read_text()
    assert '"443:443"' in compose
    assert '"80:80"' not in compose
    assert "--workers 1" in compose
    assert "alembic upgrade head" in compose
    assert "service_healthy" in compose
    assert "MAILS_ENCRYPTION_KEY" in (root / ".env.example").read_text()


def test_private_flow(api_client, session_headers):
    client, state, engine = api_client
    response = client.post(
        "/api/v1/connections",
        headers=session_headers,
        json={
            "kind": "outlook_plus",
            "name": "Test",
            "base_url": "https://mail.example",
            "api_key": "integration-test-key",
        },
    )
    assert response.status_code == 201, response.text
    connection = response.json()
    boxes = []
    for address in ("a@example.com", "b@example.com"):
        response = client.post(
            f"/api/v1/connections/{connection['id']}/mailboxes",
            headers=session_headers,
            json={"address": address},
        )
        assert response.status_code == 201, response.text
        boxes.append(response.json())
    for _ in range(2):
        response = client.post(
            f"/api/v1/mailboxes/{boxes[0]['id']}/sync", headers=session_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success"
    response = client.get("/api/v1/messages")
    assert response.status_code == 200
    messages = response.json()
    assert messages["total"] == len(messages["items"]) == 1
    message = messages["items"][0]
    assert message["verification_code"] == "123456"
    assert (
        "123456" in client.get(f"/api/v1/messages/{message['id']}").json()["body_text"]
    )
    assert state["details"] == 1
    assert len(client.get("/api/v1/mailboxes").json()) == 2
    from mails_api.models import ProviderConnection
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        assert (
            b"integration-test-key"
            not in db.scalar(select(ProviderConnection)).encrypted_secret
        )
    assert "integration-test-key" not in client.get("/api/v1/connections").text
    assert client.post(f"/api/v1/mailboxes/{boxes[0]['id']}/sync").status_code == 403
    assert client.delete("/api/v1/session", headers=session_headers).status_code == 204
    for path in ("messages", "mailboxes", "connections", "sync-runs"):
        assert client.get(f"/api/v1/{path}").status_code == 401


@pytest.mark.parametrize(
    "url", ["sqlite://", "postgresql+psycopg://localhost/production"]
)
def test_refuses_non_disposable_database(monkeypatch, request, url):
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    with pytest.raises(pytest.fail.Exception, match="Refusing setup"):
        request.getfixturevalue("api_client")
