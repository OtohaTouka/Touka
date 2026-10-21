"""ask_user 答案端點整合測試（HAX G10，ADR-0085）。

驗證 POST /ask_user/answer/{token} 交付答案、未知 token 優雅回應、需驗證身分。
"""

from src.main import app
from src.services.ask_user_store import _reset_ask_user_store, get_ask_user_store
from src.services.identity_router import get_verified_identity


def test_answer_delivers_to_registered_token(auth_client):
    _reset_ask_user_store()
    store = get_ask_user_store()
    store.register("tok-int-1")
    r = auth_client.post("/v1/ask_user/answer/tok-int-1", json={"answers": [["A"]]})
    assert r.status_code == 200
    assert r.json()["status"] == "delivered"


def test_answer_unknown_token_graceful(auth_client):
    _reset_ask_user_store()
    r = auth_client.post("/v1/ask_user/answer/unknown-token", json={"answers": [["A"]]})
    assert r.status_code == 200
    assert r.json()["status"] == "unknown_token"


def test_answer_requires_authentication(auth_client):
    app.dependency_overrides[get_verified_identity] = lambda: None
    r = auth_client.post("/v1/ask_user/answer/x", json={"answers": []})
    assert r.status_code in (401, 403)
