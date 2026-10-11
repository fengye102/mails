"""机器接口：鉴权、能力清单如实反映已落地的部分。"""


def test_health_requires_operator_token(client) -> None:
    assert client.get("/icloud-api/v1/health").status_code == 401
    assert client.get("/healthz").status_code == 200


def test_wrong_token_is_rejected(client) -> None:
    response = client.get("/icloud-api/v1/health", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_capabilities_report_only_shipped_features(client) -> None:
    response = client.get(
        "/icloud-api/v1/capabilities", headers={"Authorization": "Bearer operator-test-token"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["features"]["apple_srp_login"] is True
    # 没收信、没写别名、没授权、没调度，就不能对外声称可用。
    for name in ("aliases_read", "aliases_write", "mail_imap", "mail_web", "browser_auth", "scheduler"):
        assert body["features"][name] is False, name
    assert body["features"]["mail_netease_web"] is False
    assert body["limits"]["scheduler_default_enabled"] is False


def test_missing_operator_token_is_a_service_error(settings, engine) -> None:
    from fastapi.testclient import TestClient
    from icloud_api.app import create_app

    settings.operator_token = ""
    with TestClient(create_app(settings, engine)) as test_client:
        response = test_client.get("/icloud-api/v1/health", headers={"Authorization": "Bearer x"})
    assert response.status_code == 503
