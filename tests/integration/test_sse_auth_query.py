import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_service import identity_service

# 測的是「權杖只能放在 Authorization 標頭」。原本用 /terminal/history，2026-11-24 起它只給
# 人類管理員本人（外部審查第 12 項），改用任何登入者都能讀的端點。
_ENDPOINT = "/v1/models/lifecycle-alerts"


@pytest.fixture
def untrusted_client():
    # 藉由指定 client 參數為外網 IP，模擬非信任的遠端請求 (避免觸發 LDH 自動授權)
    with TestClient(app, client=("8.8.8.8", 1234)) as c:
        yield c


def test_sse_stream_without_auth_fails(untrusted_client):
    """沒有 Auth Header 且沒有 Query Token 的外部請求應被拒絕，回傳 401。"""
    response = untrusted_client.get(_ENDPOINT)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"


def test_sse_stream_with_query_token_fails_under_new_policy(untrusted_client):
    """外部請求在 URL Query 中提供 Token 應被拒絕 (401)，不再支援 URL Query 降級驗證。"""
    identity = AgentIdentity.for_role(
        agent_id="DebugAgent",
        role="Owner",
        capabilities=["heartbeat_awakening"]
    )
    token = identity_service.issue_token(identity)

    response = untrusted_client.get(f"{_ENDPOINT}?token={token}")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"


def test_sse_stream_with_auth_header_success(untrusted_client):
    """外部請求在 HTTP Authorization Header 中提供合法 Token 應能成功通過認證。"""
    identity = AgentIdentity.for_role(
        agent_id="DebugAgent",
        role="Owner",
        capabilities=["heartbeat_awakening"]
    )
    token = identity_service.issue_token(identity)

    response = untrusted_client.get(
        _ENDPOINT,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text

