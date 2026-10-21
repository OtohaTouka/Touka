"""設定頁「API 金鑰」分頁的資料來源：`GET /config/env` 必須列出根 `.env.example` 的六把廠商金鑰。

`GET /config/env` 的 definitions 只從 live `.env` 同目錄的 `.env.example` 產生（ADR-0243 決策 3）：
範本少了金鑰，設定頁就一律顯示未設定。本測試把 tracked 的根 `.env.example` 複製到 pytest 暫存區，
旁邊放一份合成 `.env`（只有假值），再以 `TOUKA_ENV_FILE` 指向它；絕不讀真 `.env`。
已設定的值必須回遮罩，未設定的回空字串。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT_ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

_ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True, capabilities=[])

_VENDOR_KEYS = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GROK_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "TAVILY_API_KEY",
)


def test_config_env_lists_root_vendor_keys_masked_or_empty(monkeypatch, tmp_path):
    (tmp_path / ".env.example").write_text(
        ROOT_ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    synthetic_env = tmp_path / ".env"
    synthetic_env.write_text(
        "ANTHROPIC_API_KEY=fake-anthropic-key\nGEMINI_API_KEY=\n", encoding="utf-8"
    )
    monkeypatch.setenv("TOUKA_ENV_FILE", str(synthetic_env))
    app.dependency_overrides[get_verified_identity] = lambda: _ADMIN
    try:
        resp = TestClient(app).get("/config/env")
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)

    assert resp.status_code == 200
    values = {item["key"]: item["value"] for item in resp.json()["definitions"]}
    assert {key: values.get(key) for key in _VENDOR_KEYS} == {
        "ANTHROPIC_API_KEY": "••••••••",
        "GEMINI_API_KEY": "",
        "GROK_API_KEY": "",
        "OPENAI_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "TAVILY_API_KEY": "",
    }
