"""資安回歸：自訂連線金鑰絕不可進模型 descriptor 或落地 models.json。

路由端點與憑證現由獨立的 ``CustomModelRoute`` 記憶體 SSOT 原子保存；模型合併層會丟棄
舊制 ``_source_url``／``_api_key``，持久化層仍剝除全部底線欄位作縱深防禦。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from src.services.models.manager import ModelManager  # noqa: E402
from src.services.models.routes import (  # noqa: E402
    CustomModelRoute,
    ModelRouteSnapshot,
    ModelRouteSnapshotState,
)


def _make_manager(cache: list | None = None) -> ModelManager:
    """建立乾淨的 ModelManager，跳過初始化 IO。"""
    with patch.object(ModelManager, "_load_blacklist"), \
         patch.object(ModelManager, "load_initial_config"):
        mgr = ModelManager()
    mgr._models_cache = list(cache or [])
    mgr._blacklist = []
    return mgr


def test_load_initial_config_scrubs_and_persists_legacy_route_fields():
    """重啟載入 legacy models.json 時，秘密須在任何 API 讀取前清除並覆寫乾淨檔。"""
    polluted = {
        "models": [
            {
                "id": "claude-custom",
                "object": "model",
                "owned_by": "custom",
                "_source_url": "https://legacy.example.test/v1",
                "_api_key": "sk-legacy-secret",
            }
        ]
    }

    with (
        patch.object(ModelManager, "_load_blacklist"),
        patch("src.services.models.manager.config_utils") as config,
    ):
        config.config_exists.return_value = True
        config.read_config_json.return_value = polluted
        manager = ModelManager()

    assert manager._models_cache == [
        {
            "id": "claude-custom",
            "object": "model",
            "owned_by": "custom",
        }
    ]
    config.save_config_json.assert_called_once_with(
        "models.json",
        {
            "models": [
                {
                    "id": "claude-custom",
                    "object": "model",
                    "owned_by": "custom",
                }
            ]
        },
    )


def test_get_models_defensively_strips_legacy_route_fields():
    """即使測試替身或舊 plugin 污染快取，公開模型投影也不可洩漏路由秘密。"""
    manager = _make_manager(
        cache=[
            {
                "id": "claude-custom",
                "object": "model",
                "owned_by": "custom",
                "_source_url": "https://legacy.example.test/v1",
                "_api_key": "sk-legacy-secret",
            }
        ]
    )

    with (
        patch("src.services.models.manager.agent_service.get_all_agents", return_value=[]),
        patch("src.services.models.manager.registry.health_key_missing", return_value=False),
    ):
        public_models = manager.get_models()

    assert len(public_models) == 1
    assert "_source_url" not in public_models[0]
    assert "_api_key" not in public_models[0]


def test_save_config_strips_runtime_fields_and_never_serializes_route_secret():
    """_save_config 不得序列化 runtime 欄位或獨立路由 SSOT。"""
    mgr = _make_manager(
        cache=[
            {
                "id": "openai/gpt-4o",
                "object": "model",
                "owned_by": "custom",
                "enabled": True,
                "last_seen": 123.0,
                "_transient_probe": "not-persisted",
            },
            {"id": "gpt-4o", "object": "model", "owned_by": "openai"},
        ]
    )
    mgr._route_snapshot = ModelRouteSnapshot.create(
        generation=1,
        config_fingerprint="test-fingerprint",
        active_routes={
            "openai/gpt-4o": CustomModelRoute(
                connection_id="openrouter",
                endpoint="https://openrouter.ai/api/v1/chat/completions",
                api_key="sk-super-secret-real-key",
                provider="openrouter",
            )
        },
        known_custom_model_ids=frozenset({"openai/gpt-4o"}),
        state=ModelRouteSnapshotState.READY,
    )

    captured: dict = {}

    def _capture(filename: str, data: dict) -> None:
        captured["filename"] = filename
        captured["data"] = data

    with patch(
        "src.services.models.manager.config_utils.save_config_json",
        side_effect=_capture,
    ):
        mgr._save_config()

    # 1. 持久化的每個模型都不得含任何底線前綴欄位
    persisted = captured["data"]["models"]
    for m in persisted:
        assert not any(k.startswith("_") for k in m), (
            f"持久化模型 {m.get('id')!r} 不得含底線前綴 runtime 欄位：{list(m)}"
        )

    # 2. 序列化後整份 JSON 不得出現金鑰／來源字串（縱深字串斷言）
    blob = json.dumps(captured["data"], ensure_ascii=False)
    assert "_api_key" not in blob
    assert "_source_url" not in blob
    assert "sk-super-secret-real-key" not in blob

    # 3. 非 runtime 欄位須完整保留
    gpt = next(m for m in persisted if m["id"] == "openai/gpt-4o")
    assert gpt["owned_by"] == "custom"
    assert gpt["enabled"] is True
    assert gpt["last_seen"] == 123.0

    # 4. 執行期路由仍在獨立 SSOT，未因儲存模型設定而遺失
    route = mgr.get_custom_route("openai/gpt-4o")
    assert route is not None
    assert route.api_key == "sk-super-secret-real-key"


def test_merge_discards_legacy_route_fields_from_existing_disk_loaded_model():
    """滾動升級時，舊 fetcher 的路由欄位也不得重新污染模型 descriptor。"""
    # 模擬「載入自 models.json」的條目——已被剝除 _source_url/_api_key
    mgr = _make_manager(
        cache=[
            {
                "id": "openai/gpt-4o",
                "object": "model",
                "owned_by": "custom",
                "enabled": True,
                "visibility": "visible",
                "last_seen": 1.0,
            }
        ]
    )

    # 連線發現再次回傳同一模型，這次帶 runtime 欄位
    rediscovered = [
        {
            "id": "openai/gpt-4o",
            "object": "model",
            "owned_by": "custom",
            "_source_url": "https://openrouter.ai/api",
            "_api_key": "sk-real-key",
        }
    ]

    with patch.object(mgr, "_save_config"):
        mgr._merge_models(rediscovered)

    # 不得新增重複條目
    matches = [m for m in mgr._models_cache if m["id"] == "openai/gpt-4o"]
    assert len(matches) == 1
    entry = matches[0]
    assert "_source_url" not in entry
    assert "_api_key" not in entry
    # 既有的使用者設定（enabled/visibility）須保留、last_seen 須刷新
    assert entry["enabled"] is True
    assert entry["visibility"] == "visible"
    assert entry["last_seen"] > 1.0


def test_official_provider_merge_introduces_no_underscore_fields():
    """官方供應商發現結果不帶 _-欄位；既有條目經其再合併不得被汙染。"""
    mgr = _make_manager(
        cache=[{"id": "gpt-4o", "object": "model", "owned_by": "openai", "last_seen": 1.0}]
    )
    with patch.object(mgr, "_save_config"):
        mgr._merge_models([{"id": "gpt-4o", "object": "model", "owned_by": "openai"}])

    entry = next(m for m in mgr._models_cache if m["id"] == "gpt-4o")
    assert not any(k.startswith("_") for k in entry)
