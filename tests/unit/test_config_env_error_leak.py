"""TDD — E#21：POST /config/env 寫檔失敗時，500 回應不得洩漏原始例外字串（路徑/內部）。

原本 `ToukaException(SYSTEM_ERROR, error_output=str(e))` 會把 FileNotFoundError 的完整路徑等
內部細節塞進回應（雖僅 admin 可觸及）。修法：str(e) 只記 log，回應回乾淨的 SYSTEM_ERROR。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity


def test_config_env_write_failure_does_not_leak_exception(monkeypatch, tmp_path):
    admin = AgentIdentity.for_role(
        agent_id="DebugAgent", role="admin", is_human=True, capabilities=[]
    )
    app.dependency_overrides[get_verified_identity] = lambda: admin
    # 指向不存在的父目錄 → 寫入時 open('w') 失敗，例外訊息含此可辨識路徑片段。
    bad = tmp_path / "no_such_dir" / "SECRETLEAK.env"
    monkeypatch.setenv("TOUKA_ENV_FILE", str(bad))
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/config/env", json={"settings": {"LOG_LEVEL": "DEBUG"}})
        assert resp.status_code >= 500
        body = resp.text
        assert "SECRETLEAK" not in body, "raw exception path leaked to client"
        assert "no_such_dir" not in body, "raw exception path leaked to client"
        assert "error_output" not in body, "internal error_output field leaked to client"
    finally:
        app.dependency_overrides.clear()
