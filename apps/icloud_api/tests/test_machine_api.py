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
    features = body["features"]
    assert features["aliases_read"] == {"shipped": True, "verified_live": False}
    assert features["browser_auth"]["shipped"] is True
    assert features["apple_srp_login"]["shipped"] is True
    # 没收信、没写别名、没调度、没接 Apple Account 会话，就不能对外声称可用。
    for name in ("aliases_write", "mail_imap", "mail_web", "scheduler", "apple_account_session", "mail_netease_web"):
        assert features[name]["shipped"] is False, name
    # 还没上真机：任何链路都不能声称已在真实账号验证过。
    assert all(not state["verified_live"] for state in features.values())
    assert body["limits"]["scheduler_default_enabled"] is False


def test_live_verified_features_must_be_a_subset_of_shipped() -> None:
    from icloud_api.routes.machine import LIVE_VERIFIED_FEATURES, SHIPPED_FEATURES

    # 真机验证过的链路必然也是已落地的；写反了就是谎报能力。
    assert LIVE_VERIFIED_FEATURES <= set(SHIPPED_FEATURES)


def test_missing_operator_token_is_a_service_error(settings, engine) -> None:
    from fastapi.testclient import TestClient
    from icloud_api.app import create_app

    settings.operator_token = ""
    with TestClient(create_app(settings, engine)) as test_client:
        response = test_client.get("/icloud-api/v1/health", headers={"Authorization": "Bearer x"})
    assert response.status_code == 503
