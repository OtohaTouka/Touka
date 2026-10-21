"""TDD — Phase 0：連線 apiKey 必須 GET 遮罩、POST write-only。

`GET /v1/ui/connections` 先前直接回傳明文 apiKey（與已遮罩的 /config/env 不一致），
構成金鑰外洩面。此測試回歸守護：
  - GET 永不回真值：已設定 → 遮罩佔位；未設定 → 維持空/None。
  - POST write-only：遮罩佔位的 apiKey 代表「未變更」→ 以 id 對應保留磁碟既有真值；
    真值覆寫；空字串清除。遮罩佔位絕不落盤。

與前端閉環：前端編輯未動金鑰時，把 GET 取得的遮罩值原樣送回（applyConnectionEdit
在 changeKey=false 時保留 existing.apiKey＝遮罩），POST 端據此還原真值。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager
from src.utils.secrets import MASK, is_masked, mask_secret

OWNER = AgentIdentity.for_role(agent_id="OwnerUser", role="Owner", is_human=True, capabilities=[])


@pytest.fixture
def owner_client(tmp_path, monkeypatch):
    """以 Owner 身分、connections.json 指向 tmp 檔的測試客戶端。"""
    cfg = tmp_path / "connections.json"
    monkeypatch.setattr("src.routers.connections.CONFIG_PATH", str(cfg))
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    with TestClient(app) as client:
        yield client, cfg
    app.dependency_overrides.clear()


def _seed(cfg, connections: list[dict]) -> None:
    resource_manager.write_json(str(cfg), {"connections": connections})


def _conn(cid: str, key, **over) -> dict:
    return {
        "id": cid,
        "name": over.get("name", cid),
        "url": over.get("url", "https://api.example.com/v1"),
        "apiKey": key,
        # 預設 custom（非官方上游）→ 不觸發 Q2 角色分離守衛；需測重疊時各測試自設 provider
        "provider": over.get("provider", "custom"),
        "protocol": over.get("protocol", "openai"),
        "enabled": over.get("enabled", True),
    }


# ---- 純函式（遮罩 SSOT） ----

def test_mask_secret_masks_when_set():
    assert mask_secret("sk-secret-123") == MASK


def test_mask_secret_passthrough_empty():
    assert mask_secret("") == ""
    assert mask_secret(None) is None


def test_is_masked():
    assert is_masked(MASK) is True
    assert is_masked("sk-real") is False
    assert is_masked("") is False
    assert is_masked(None) is False


# ---- GET 遮罩 ----

def test_get_masks_set_key_never_echoes_real(owner_client):
    client, cfg = owner_client
    _seed(cfg, [
        _conn("c1", "sk-REAL-secret"),
        _conn("c2", None, provider="ollama", url="http://localhost:11434"),
    ])
    resp = client.get("/v1/ui/connections")
    assert resp.status_code == 200
    body = resp.json()
    by_id = {c["id"]: c for c in body}
    assert by_id["c1"]["apiKey"] == MASK            # 已設定 → 遮罩
    assert "sk-REAL-secret" not in str(body)        # 真值絕不出現在回應任何角落
    assert by_id["c2"]["apiKey"] in (None, "")      # 未設定 → 維持空


# ---- POST write-only ----

def test_post_masked_key_preserves_existing(owner_client):
    client, cfg = owner_client
    _seed(cfg, [_conn("c1", "sk-OLD")])
    # 前端把未變更連線的遮罩值原樣送回
    resp = client.post("/v1/ui/connections", json=[_conn("c1", MASK)])
    assert resp.status_code == 200
    on_disk = resource_manager.read_json(str(cfg))["connections"][0]
    assert on_disk["apiKey"] == "sk-OLD"            # 既有真值保留
    assert MASK not in cfg.read_text(encoding="utf-8")  # 遮罩佔位絕不落盤


def test_post_real_key_overwrites(owner_client):
    client, cfg = owner_client
    _seed(cfg, [_conn("c1", "sk-OLD")])
    resp = client.post("/v1/ui/connections", json=[_conn("c1", "sk-NEW")])
    assert resp.status_code == 200
    on_disk = resource_manager.read_json(str(cfg))["connections"][0]
    assert on_disk["apiKey"] == "sk-NEW"


def test_post_new_connection_writes_real_key(owner_client):
    client, cfg = owner_client
    _seed(cfg, [])
    resp = client.post("/v1/ui/connections", json=[_conn("c2", "sk-FRESH", provider="custom")])
    assert resp.status_code == 200
    on_disk = resource_manager.read_json(str(cfg))["connections"][0]
    assert on_disk["id"] == "c2"
    assert on_disk["apiKey"] == "sk-FRESH"


def test_post_empty_string_clears_key(owner_client):
    client, cfg = owner_client
    _seed(cfg, [_conn("c1", "sk-OLD")])
    # 明確空字串（非遮罩）→ 視為清除
    resp = client.post("/v1/ui/connections", json=[_conn("c1", "")])
    assert resp.status_code == 200
    on_disk = resource_manager.read_json(str(cfg))["connections"][0]
    assert on_disk["apiKey"] in (None, "")


# ---- SSRF 守衛（Phase 2b：拒絕指向 metadata/link-local 的連線網址） ----

def test_post_rejects_metadata_url(owner_client):
    client, cfg = owner_client
    _seed(cfg, [])
    resp = client.post(
        "/v1/ui/connections",
        json=[_conn("evil", "k", url="http://169.254.169.254/latest/meta-data/")],
    )
    assert 400 <= resp.status_code < 500          # VALIDATION_ERROR（非 500）
    assert not (resource_manager.read_json(str(cfg)) or {}).get("connections")  # 未落盤


def test_post_rejects_non_http_scheme(owner_client):
    client, cfg = owner_client
    _seed(cfg, [])
    resp = client.post(
        "/v1/ui/connections",
        json=[_conn("evil", "k", url="file:///etc/passwd", provider="custom")],
    )
    assert 400 <= resp.status_code < 500


def test_post_allows_localhost_url(owner_client):
    """自架 Ollama localhost 為合法目標，必須放行。"""
    client, cfg = owner_client
    _seed(cfg, [])
    resp = client.post(
        "/v1/ui/connections",
        json=[_conn("ollama", None, url="http://localhost:11434", provider="ollama")],
    )
    assert resp.status_code == 200
    on_disk = resource_manager.read_json(str(cfg))["connections"][0]
    assert on_disk["url"] == "http://localhost:11434"


def test_post_rejects_empty_url(owner_client):
    """空網址明確報錯（不再存得下、刷新時才靜默失敗）。"""
    client, cfg = owner_client
    _seed(cfg, [])
    resp = client.post(
        "/v1/ui/connections",
        json=[_conn("c1", "k", url="", provider="custom")],
    )
    assert 400 <= resp.status_code < 500
    assert not (resource_manager.read_json(str(cfg)) or {}).get("connections")


# ---- Q2 守衛：角色分離 ＋ 重複偵測（Phase 4b） ----

def test_post_blocks_overlap_with_system_key(owner_client, monkeypatch):
    """官方供應商已在系統金鑰設定 → 連線頁擋（角色分離）。"""
    client, cfg = owner_client
    _seed(cfg, [])
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", "sk-system", raising=False)
    resp = client.post(
        "/v1/ui/connections",
        json=[_conn("c1", "sk-x", url="https://api.deepseek.com/v1", provider="deepseek")],
    )
    assert 400 <= resp.status_code < 500


def test_post_allows_provider_connection_without_system_key(owner_client, monkeypatch):
    """同供應商但系統未設金鑰 → 不算重疊，放行。"""
    client, cfg = owner_client
    _seed(cfg, [])
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", None, raising=False)
    resp = client.post(
        "/v1/ui/connections",
        json=[_conn("c1", "sk-conn", url="https://api.deepseek.com/v1", provider="deepseek")],
    )
    assert resp.status_code == 200


def test_post_blocks_duplicate_endpoint(owner_client):
    """提交清單內 (provider, url) 相同 → 擋（網址正規化後比對）。"""
    client, cfg = owner_client
    _seed(cfg, [])
    resp = client.post(
        "/v1/ui/connections",
        json=[
            _conn("a", "k1", url="http://localhost:11434", provider="ollama"),
            _conn("b", None, url="http://localhost:11434/", provider="ollama"),
        ],
    )
    assert 400 <= resp.status_code < 500


def test_post_overlap_guard_is_case_insensitive(owner_client, monkeypatch):
    """provider 大小寫變化不可繞過角色分離守衛（複審 #4b 補強）。"""
    client, cfg = owner_client
    _seed(cfg, [])
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", "sk-system", raising=False)
    resp = client.post(
        "/v1/ui/connections",
        json=[_conn("c1", "sk-x", url="https://api.deepseek.com/v1", provider="DeepSeek")],
    )
    assert 400 <= resp.status_code < 500


def test_post_duplicate_endpoint_case_insensitive(owner_client):
    """provider 大小寫變化不可繞過重複端點偵測。"""
    client, cfg = owner_client
    _seed(cfg, [])
    resp = client.post(
        "/v1/ui/connections",
        json=[
            _conn("a", "k1", url="http://localhost:11434", provider="ollama"),
            _conn("b", "k2", url="http://localhost:11434", provider="Ollama"),
        ],
    )
    assert 400 <= resp.status_code < 500


def test_post_blocks_duplicate_api_key(owner_client):
    """提交清單內同一把真值金鑰用於多個連線 → 擋。"""
    client, cfg = owner_client
    _seed(cfg, [])
    resp = client.post(
        "/v1/ui/connections",
        json=[
            _conn("a", "sk-samekey", url="http://10.0.0.1:8000", provider="custom"),
            _conn("b", "sk-samekey", url="http://10.0.0.2:8000", provider="custom"),
        ],
    )
    assert 400 <= resp.status_code < 500
