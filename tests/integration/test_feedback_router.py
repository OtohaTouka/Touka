"""Feedback Router 整合測試（HAX G15/G6，ADR-0084）。

驗證提交/還原/撤回端點與授權閘（檢視端點僅 Owner/admin）。沿用 conftest 的
auth_client（預設 DebugAgent/Owner），符合「測試只碰 Debug/Dispatch 身分」。
"""

from src.main import app
from src.models.identity import AgentIdentity  # noqa: F401  (型別參考；保持與其他整合測試一致)
from src.services.identity_router import get_verified_identity


def test_submit_and_get_session(auth_client):
    r = auth_client.post(
        "/v1/feedback/submit",
        json={"session_id": "s1", "message_id": "m1", "feedback_type": "up"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "success"

    g = auth_client.get("/v1/feedback/session/s1")
    assert g.status_code == 200
    assert g.json()["feedbacks"]["m1"]["type"] == "up"


def test_submit_requires_authentication(auth_client):
    app.dependency_overrides[get_verified_identity] = lambda: None
    r = auth_client.post(
        "/v1/feedback/submit",
        json={"session_id": "s1", "message_id": "m1", "feedback_type": "up"},
    )
    assert r.status_code in (401, 403)


def test_dislike_with_reason_then_retract(auth_client):
    auth_client.post(
        "/v1/feedback/submit",
        json={
            "session_id": "s2",
            "message_id": "m2",
            "feedback_type": "down",
            "reason": "有害內容回報",
        },
    )
    g = auth_client.get("/v1/feedback/session/s2")
    assert g.json()["feedbacks"]["m2"]["reason"] == "有害內容回報"

    auth_client.post(
        "/v1/feedback/submit",
        json={"session_id": "s2", "message_id": "m2", "feedback_type": "none"},
    )
    g2 = auth_client.get("/v1/feedback/session/s2")
    assert "m2" not in g2.json()["feedbacks"]


def test_recent_is_owner_only(auth_client, mock_identity):
    # 預設 Owner 可讀
    assert auth_client.get("/v1/feedback/recent").status_code == 200
    # 非 Owner（Manual）→ 403
    app.dependency_overrides[get_verified_identity] = lambda: mock_identity(
        agent_id="DebugAgent", role="Manual"
    )
    assert auth_client.get("/v1/feedback/recent").status_code == 403


def test_invalid_feedback_type_rejected(auth_client):
    r = auth_client.post(
        "/v1/feedback/submit",
        json={"session_id": "s3", "message_id": "m3", "feedback_type": "bogus"},
    )
    # Literal 驗證 → 422
    assert r.status_code == 422
