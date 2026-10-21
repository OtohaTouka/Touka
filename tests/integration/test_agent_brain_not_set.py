"""model-p0 U7：代理人沒有主要模型時回 AGENT_BRAIN_NOT_SET（409），不偷換寫死模型。

錯誤發生在 pipeline（SoulLayer）、串流開始之前，所以前端收到非 200 回應，
body 為 ToukaException 的標準形狀 ``{"detail": {"code","id","message","i18n"}}``。

靈魂頁存檔設主要模型的規則見 ADR-0250（只有人類存檔、新值在模型清單裡才設），
所以這裡的存檔一律用人類 admin＋步升密碼，模型名先種進沙盒模型清單。
"""

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from src.config import PROXY_ROOT
from src.main import app
from src.models.identity import AgentIdentity
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.models.routes import (
    CustomModelRoute,
    ModelRouteSnapshot,
    ModelRouteSnapshotState,
)
from src.utils.brain_persistence import get_active_brain, set_active_brain

AGENT_ID = "DebugAgent"
_STEPUP_PW = "BrainNotSetPass123!"
_PAYLOADS = {
    "/v1/chat/completions": {"model": AGENT_ID, "messages": [{"role": "user", "content": "hi"}]},
    "/v1/messages": {
        "model": AGENT_ID,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    },
}


def _expected_definition() -> dict:
    example = PROXY_ROOT / "config" / "errors.json.example"
    return json.loads(example.read_text(encoding="utf-8"))["AGENT_BRAIN_NOT_SET"]


@pytest.mark.parametrize("path", sorted(_PAYLOADS))
def test_agent_without_brain_returns_conflict(auth_client, global_test_sandbox, path):
    """沒有 active_brain 的靈魂 → 409 AGENT_BRAIN_NOT_SET，且完全不分派到任何模型。"""
    soul_dir = Path(global_test_sandbox) / "soul" / AGENT_ID
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text(f"I am {AGENT_ID}", encoding="utf-8")
    (soul_dir / "config.json").write_text('{"is_toukasoul": true}', encoding="utf-8")

    dispatch = AsyncMock()
    with (
        patch("memory_server.config_utils.get_app_data_dir", return_value=str(global_test_sandbox)),
        patch("src.services.proxy_service.dispatch_request", dispatch),
    ):
        response = auth_client.post(
            path, json=_PAYLOADS[path], headers={"X-Touka-Soul-ID": AGENT_ID}
        )

    assert response.status_code == 409, response.text
    definition = _expected_definition()
    assert definition["status"] == 409
    assert response.json() == {
        "detail": {
            "code": "AGENT_BRAIN_NOT_SET",
            "id": definition["id"],
            "message": definition["msg"],
            "i18n": "error.model.agent_brain_not_set",
        }
    }
    dispatch.assert_not_called()


def _soul(sandbox: str) -> Path:
    soul_dir = Path(sandbox) / "soul" / AGENT_ID
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "config.json").write_text('{"is_toukasoul": true}', encoding="utf-8")
    return soul_dir


def _save(client, config: dict):
    return client.post(f"/v1/souls/{AGENT_ID}", json={"config": config})


def _seed_models(*model_ids: str) -> None:
    model_manager.set_models([{"id": model_id, "owned_by": "anthropic"} for model_id in model_ids])


def _human_save(config: dict):
    """人類 admin＋步升密碼存檔；結束後換回原本的身分覆寫（聊天仍用 auth_client 的身分）。"""
    user = AuthService().create_user(f"brain-{uuid.uuid4().hex}@test.com", _STEPUP_PW, "admin")
    human = AgentIdentity.for_role(user.id, "admin", is_human=True)
    previous = app.dependency_overrides.get(get_verified_identity)
    app.dependency_overrides[get_verified_identity] = lambda: human
    try:
        return TestClient(app, raise_server_exceptions=False).post(
            f"/v1/souls/{AGENT_ID}", json={"config": config, "admin_password": _STEPUP_PW}
        )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_verified_identity, None)
        else:
            app.dependency_overrides[get_verified_identity] = previous


def test_saving_base_model_recovers_agent_without_brain(auth_client, global_test_sandbox):
    """409 → 人類在靈魂頁存檔帶 base_model → 下一次聊天分派到所選模型（review-3 HIGH-1，甲）。"""
    _soul(global_test_sandbox)
    _seed_models("claude-x")
    path = "/v1/chat/completions"
    headers = {"X-Touka-Soul-ID": AGENT_ID}
    dispatch = AsyncMock(return_value={"ok": True})
    with patch("src.services.proxy_service.dispatch_request", dispatch):
        assert auth_client.post(path, json=_PAYLOADS[path], headers=headers).status_code == 409
        saved = _human_save({"base_model": "claude-x"})
        assert saved.status_code == 200, saved.text
        assert auth_client.post(path, json=_PAYLOADS[path], headers=headers).status_code == 200
    assert dispatch.call_args.args[0] == "claude-x"


def test_human_changing_base_model_switches_brain(global_test_sandbox):
    """已經有主要模型：人類把「你指定的」改成清單裡的另一個，主要模型跟著換（ADR-0250）。"""
    _soul(global_test_sandbox)
    _seed_models("claude-old", "claude-new")
    set_active_brain(AgentIdentity(agent_id=AGENT_ID), "claude-old")
    saved = _human_save({"base_model": "claude-new"})
    assert saved.status_code == 200, saved.text
    assert get_active_brain(AgentIdentity(agent_id=AGENT_ID)) == "claude-new"


def test_saving_without_base_model_does_not_set_brain(auth_client, global_test_sandbox):
    """存檔沒帶 base_model：不呼叫 set_active_brain。"""
    _soul(global_test_sandbox)
    with patch("src.routers.souls.set_active_brain") as setter:
        saved = _save(auth_client, {})
    assert saved.status_code == 200, saved.text
    setter.assert_not_called()


@pytest.mark.parametrize("placeholder", ["未配置", "錯誤"])
def test_placeholder_base_model_is_rejected(auth_client, global_test_sandbox, placeholder):
    """顯示字串被當 base_model 送回：404、不寫設定也不寫主要模型，之後存有效模型仍救得回。"""
    soul_dir = _soul(global_test_sandbox)
    _seed_models("claude-x")
    before = (soul_dir / "config.json").read_bytes()
    rejected = _human_save({"base_model": placeholder})
    assert rejected.status_code == 404, rejected.text
    assert rejected.json()["detail"]["code"] == "MODEL_NOT_FOUND"
    assert (soul_dir / "config.json").read_bytes() == before
    assert get_active_brain(AgentIdentity(agent_id=AGENT_ID)) is None
    path, headers = "/v1/chat/completions", {"X-Touka-Soul-ID": AGENT_ID}
    chat = auth_client.post(path, json=_PAYLOADS[path], headers=headers)
    assert chat.json()["detail"]["code"] == "AGENT_BRAIN_NOT_SET"
    assert _human_save({"base_model": "claude-x"}).status_code == 200
    assert get_active_brain(AgentIdentity(agent_id=AGENT_ID)) == "claude-x"


def test_active_custom_connection_id_is_saved_as_brain(global_test_sandbox, monkeypatch):
    """啟用中的自訂連線模型（原名有大寫）可以存成主要模型；聊天端的大小寫由 ADR-0250 A5 處理。"""
    _soul(global_test_sandbox)
    upper = "Qwen/Qwen2.5-72B-Instruct"
    route = CustomModelRoute(
        connection_id="c", endpoint="http://127.0.0.1:1", api_key="", provider="X"
    )
    snapshot = ModelRouteSnapshot.create(
        generation=1,
        config_fingerprint="sandbox",
        active_routes={upper: route},
        state=ModelRouteSnapshotState.READY,
    )
    monkeypatch.setattr(model_manager, "_route_snapshot", snapshot)
    saved = _human_save({"base_model": upper})
    assert saved.status_code == 200, saved.text
    assert get_active_brain(AgentIdentity(agent_id=AGENT_ID)) == upper
