import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.custom_models import user_service
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity


@pytest.fixture
def test_users():
    """在測試沙箱的 users.json 中建立兩個 Owner 使用者。"""
    auth_service = AuthService()
    
    # 清空沙箱中既有的使用者以確保環境乾淨
    def _clear(users):
        users.clear()
    auth_service._atomic_users_write(_clear)
    
    # 建立 Owner A
    user_a = auth_service.create_user(
        email="owner_a@touka.io",
        password="password123",
        role="Owner"
    )
    # 建立 Owner B
    user_b = auth_service.create_user(
        email="owner_b@touka.io",
        password="password123",
        role="Owner"
    )
    return user_a, user_b


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_custom_model_idor_lifecycle(client, test_users, monkeypatch):
    """TDD Test: Verify IDOR privilege boundaries and dynamic owned_agents updating."""
    user_a, user_b = test_users
    auth_service = AuthService()
    
    # 使用 monkeypatch 安全修改屬性以避免全域污染
    monkeypatch.setattr(user_service.auth_service, "users_file_path", auth_service.users_file_path)
    
    # 1. 以 Owner A 身分登入/建立模型
    identity_a = AgentIdentity.for_role(agent_id=user_a.id, role="Owner", is_human=True)
    app.dependency_overrides[get_verified_identity] = lambda: identity_a
    
    model_id = "test-model-a"
    
    # 建立自定義模型 model-a
    resp = client.post(f"/ui/custom_models/{model_id}", json={
        "name": "Model A",
        "description": "Owner A's model",
        "systemPrompt": "You are Owner A's assistant.",
        "config": {"base_model": "gemini-2.5-flash"}
    })
    if resp.status_code != 200:
        pytest.fail(f"CREATE FAILED: {resp.status_code} - {resp.text}")
    
    # 驗證 model-a 自動註冊至 Owner A 的 owned_agents 清單中 (SSOT)
    db_user_a = auth_service.get_user_by_id(user_a.id)
    assert model_id in db_user_a.owned_agents
    
    # 2. 切換為 Owner B 身分，嘗試平行越權
    identity_b = AgentIdentity.for_role(agent_id=user_b.id, role="Owner", is_human=True)
    app.dependency_overrides[get_verified_identity] = lambda: identity_b
    
    # 2.1 嘗試讀取 B 限制的模型
    resp_get = client.get(f"/ui/custom_models/{model_id}")
    assert resp_get.status_code == 403 or "AUTH_PERMISSION_DENIED" in resp_get.text
    
    # 2.2 嘗試修改 B 限制的模型
    resp_post = client.post(f"/ui/custom_models/{model_id}", json={
        "name": "Hacked Model",
        "description": "Injected by Owner B",
        "systemPrompt": "You are hacked.",
        "config": {}
    })
    assert resp_post.status_code == 403 or "AUTH_PERMISSION_DENIED" in resp_post.text
    
    # 2.3 嘗試重命名 B 限制的模型
    resp_rename = client.post(f"/ui/custom_models/{model_id}/rename", json={
        "new_id": "hacked-model-new"
    })
    assert resp_rename.status_code == 403 or "AUTH_PERMISSION_DENIED" in resp_rename.text
    
    # 2.4 嘗試刪除 B 限制的模型
    resp_delete = client.delete(f"/ui/custom_models/{model_id}")
    assert resp_delete.status_code == 403 or "AUTH_PERMISSION_DENIED" in resp_delete.text
    
    # 2.5 驗證 Owner B 列出模型時為空 (不應枚舉出他人模型)
    resp_list = client.get("/ui/custom_models")
    assert resp_list.status_code == 200
    models_list = resp_list.json().get("models", [])
    assert not any(m["id"] == model_id for m in models_list)
    
    # 3. 切換回 Owner A 身分，執行合法變更
    app.dependency_overrides[get_verified_identity] = lambda: identity_a
    
    # 3.1 驗證 Owner A 可以在清單中看見自己的模型
    resp_list_a = client.get("/ui/custom_models")
    models_list_a = resp_list_a.json().get("models", [])
    assert any(m["id"] == model_id for m in models_list_a)
    
    # 3.2 重新命名模型
    new_model_id = "test-model-a-renamed"
    resp_rename_a = client.post(f"/ui/custom_models/{model_id}/rename", json={
        "new_id": new_model_id
    })
    assert resp_rename_a.status_code == 200
    
    # 驗證 owned_agents 同步更新，舊的移除，新的加入
    db_user_a_updated = auth_service.get_user_by_id(user_a.id)
    assert model_id not in db_user_a_updated.owned_agents
    assert new_model_id in db_user_a_updated.owned_agents
    
    # 3.3 刪除模型
    resp_delete_a = client.delete(f"/ui/custom_models/{new_model_id}")
    assert resp_delete_a.status_code == 200
    
    # 驗證 owned_agents 中的模型已被清除
    db_user_a_final = auth_service.get_user_by_id(user_a.id)
    assert new_model_id not in db_user_a_final.owned_agents
    
    app.dependency_overrides.clear()
