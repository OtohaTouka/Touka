"""G07（守衛涵蓋缺口待辦）— 靈魂模型註冊必須經 model_manager 單例（不得繞快取直寫）。

缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G07）：
    ADR-0145：靈魂模型註冊/註銷須經 `model_manager.register_soul_model` /
    `unregister_soul_model`（同時異動 _models_cache 與磁碟）——永不得回退成只寫
    models.json、繞過快取的直寫。否則背景 `_save_config`（check_models_health /
    mark_model_seen）會以過時快取整包覆蓋、靜默抹除 `ToukaSoul<id>`＝正式環境資料流失
    ＋隨機次序 flake 根因。

    此不變式**先前零測試**：唯一 create 測試把 _models_cache 歸零使背景健檢早退、只驗
    磁碟面；若有人把 souls 的 `_update_models_config` 改回直寫，全套件照綠。

本檔兩鎖：① register 的快取權威契約（註冊後經一次 _save_config→reload 仍存活）；
② 源碼守衛——souls `_update_models_config` 必須走 model_manager，不得直寫繞快取。
"""

from __future__ import annotations

import inspect

from src.routers import souls
from src.services.models import model_manager

_SOUL = "G07TestSoul"
_MODEL_ID = f"ToukaSoul{_SOUL}"


def test_register_soul_model_is_cache_authoritative_survives_save():
    """註冊後條目進 _models_cache＝權威；一次背景 _save_config→自磁碟 reload 仍存活。"""
    model_manager.register_soul_model(_SOUL)
    assert any(m.get("id") == _MODEL_ID for m in model_manager._models_cache), (  # noqa: SLF001
        "register_soul_model 未把條目寫入 _models_cache＝繞快取的直寫回歸"
    )

    # 模擬背景健檢的落盤（_save_config 以 _models_cache 為權威整包 dump）
    model_manager._save_config()  # noqa: SLF001
    model_manager.load_initial_config()  # 自磁碟重載

    assert any(m.get("id") == _MODEL_ID for m in model_manager._models_cache), (  # noqa: SLF001
        "註冊的靈魂模型被背景 _save_config 抹除＝過時快取整包覆蓋（正式資料流失回歸）"
    )
    model_manager.unregister_soul_model(_SOUL)  # 清理


def test_souls_update_models_config_goes_through_model_manager():
    """源碼守衛：souls._update_models_config 必須呼叫 model_manager.register/unregister_soul_model，
    且不得以 resource_manager 直寫／save_config_json 繞過快取（直接封死本 ADR 修的回歸）。"""
    src = inspect.getsource(souls._update_models_config)  # noqa: SLF001
    assert "register_soul_model" in src, "未走 model_manager.register_soul_model"
    assert "unregister_soul_model" in src, "未走 model_manager.unregister_soul_model"
    # 以下為程式呼叫模式（docstring 只提『resource_manager 直接寫檔』無 .write／save_config_json）
    assert "resource_manager.write" not in src, "偵測到繞快取的直寫（resource_manager.write）"
    assert "save_config_json" not in src, "偵測到繞 model_manager 的直接落盤（save_config_json）"


def _cache_entries(model_id: str) -> list[dict]:
    return [m for m in model_manager._models_cache if m.get("id") == model_id]  # noqa: SLF001


def test_register_soul_model_is_idempotent():
    """G20（ADR-0145）：重複註冊已存在項不得插入重複條目（避免 models.json 重覆模型列）。"""
    model_manager.register_soul_model("G20Idem")
    model_manager.register_soul_model("G20Idem")  # 重複註冊
    assert len(_cache_entries("ToukaSoulG20Idem")) == 1, "重複註冊插入了重複條目（冪等破壞）"
    model_manager.unregister_soul_model("G20Idem")


def test_unregister_absent_soul_is_noop():
    """G20（ADR-0145）：反註冊不存在項＝零寫入 no-op（create 回滾與刪缺項所倚賴）。"""
    before = list(model_manager._models_cache)  # noqa: SLF001
    model_manager.unregister_soul_model("G20NeverExisted")  # 不存在
    assert model_manager._models_cache == before, "反註冊不存在項不得改動快取"  # noqa: SLF001


def test_register_soul_model_inserts_bare_entry():
    """G22（ADR-0145）：register 必須插入『裸』條目 {"id","owned_by"}，不寫 enabled/visibility
    （此二欄由 get_models 讀取時補預設；寫入即與舊直寫行為漂移）。"""
    model_manager.register_soul_model("G22Bare")
    entry = _cache_entries("ToukaSoulG22Bare")[0]
    assert set(entry.keys()) == {"id", "owned_by"}, (
        f"register 寫入非裸條目 {sorted(entry.keys())}"
        "——enabled/visibility 應由 get_models 讀取時補（行為漂移）"
    )
    model_manager.unregister_soul_model("G22Bare")
