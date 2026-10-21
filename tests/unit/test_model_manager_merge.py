"""測試 ModelManager._merge_models 合併邏輯.

驗證新模型加入、既有模型設定保留、黑名單過濾、last_seen 更新。
"""

from unittest.mock import patch

from src.services.models.manager import ModelManager


def _make_manager_with_cache(cache: list, blacklist: list | None = None) -> ModelManager:
    """建立一個帶有預設快取的 ModelManager (跳過初始化 IO)。"""
    with patch.object(ModelManager, "_load_blacklist"), \
         patch.object(ModelManager, "load_initial_config"):
        mgr = ModelManager()
    mgr._models_cache = list(cache)  # 深拷貝避免污染
    mgr._blacklist = blacklist or []
    return mgr


def test_merge_adds_new_models():
    """新模型應被加入快取，帶有預設設定。"""
    mgr = _make_manager_with_cache([])

    new_models = [
        {"id": "gemini-2.5-flash", "object": "model", "owned_by": "google"},
        {"id": "gemini-2.5-pro", "object": "model", "owned_by": "google"},
    ]
    mgr._merge_models(new_models)

    assert len(mgr._models_cache) == 2
    for m in mgr._models_cache:
        assert m["enabled"] is True
        assert m["visibility"] == "visible"
        assert "last_seen" in m


def test_merge_preserves_existing_settings():
    """既有模型的 enabled/visibility 設定不應被覆蓋。"""
    existing = [
        {
            "id": "gemini-2.5-flash",
            "object": "model",
            "owned_by": "google",
            "enabled": False,
            "visibility": "hidden",
            "last_seen": 1000.0,
        }
    ]
    mgr = _make_manager_with_cache(existing)

    new_models = [
        {"id": "gemini-2.5-flash", "object": "model", "owned_by": "google"},
    ]
    mgr._merge_models(new_models)

    assert len(mgr._models_cache) == 1
    m = mgr._models_cache[0]
    assert m["enabled"] is False
    assert m["visibility"] == "hidden"
    # last_seen 應被更新
    assert m["last_seen"] > 1000.0


def test_merge_filters_blacklisted():
    """黑名單中的模型不應被加入。"""
    mgr = _make_manager_with_cache([], blacklist=["deprecated-model"])

    new_models = [
        {"id": "deprecated-model-v1", "object": "model", "owned_by": "google"},
        {"id": "gemini-2.5-flash", "object": "model", "owned_by": "google"},
    ]
    mgr._merge_models(new_models)

    assert len(mgr._models_cache) == 1
    assert mgr._models_cache[0]["id"] == "gemini-2.5-flash"


def test_merge_mixed_new_and_existing():
    """混合場景：既有 + 新增 + 黑名單。"""
    existing = [
        {
            "id": "gpt-4o",
            "object": "model",
            "owned_by": "openai",
            "enabled": True,
            "visibility": "visible",
            "last_seen": 500.0,
        }
    ]
    mgr = _make_manager_with_cache(existing, blacklist=["gpt-3.5"])

    new_models = [
        {"id": "gpt-4o", "object": "model", "owned_by": "openai"},
        {"id": "gpt-4o-mini", "object": "model", "owned_by": "openai"},
        {"id": "gpt-3.5-turbo", "object": "model", "owned_by": "openai"},
    ]
    mgr._merge_models(new_models)

    ids = [m["id"] for m in mgr._models_cache]
    assert "gpt-4o" in ids
    assert "gpt-4o-mini" in ids
    assert "gpt-3.5-turbo" not in ids  # 黑名單
    assert len(mgr._models_cache) == 2

    # gpt-4o 的設定應被保留
    gpt4o = next(m for m in mgr._models_cache if m["id"] == "gpt-4o")
    assert gpt4o["enabled"] is True
    assert gpt4o["last_seen"] > 500.0
