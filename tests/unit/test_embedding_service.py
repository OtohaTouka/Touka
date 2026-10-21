"""TDD — bug①：嵌入模型立即下載＋進度＋切換防呆（embedding_service）。

下載＝安全新增（per-model 快取、絕不刪既有、不 re-embed）；切換＝偵測既有記憶時警告/封鎖。
全程 mock huggingface_hub 與記憶系統，不下載真模型、不碰真 data/。
"""
from __future__ import annotations

import pytest
from dotenv import get_key
from src.services import embedding_service as es

_NET_ERR = RuntimeError("network down")  # 模組級：raise 變數避免 TRY003（字面訊息）


@pytest.fixture(autouse=True)
def _clean_state():
    es.reset_download_state()
    yield
    es.reset_download_state()


# --- repo id 解析 ---

def test_resolve_repo_id_prepends_sentence_transformers():
    assert es.resolve_repo_id("all-MiniLM-L6-v2") == "sentence-transformers/all-MiniLM-L6-v2"


def test_resolve_repo_id_keeps_namespaced():
    assert es.resolve_repo_id("intfloat/multilingual-e5-base") == "intfloat/multilingual-e5-base"


# --- 防呆①：已在快取 → 不重下；下載中 → 不重複啟動 ---

def test_begin_download_already_cached_no_spawn(monkeypatch):
    monkeypatch.setattr(es, "is_cached", lambda m: True)
    state, should_spawn = es.begin_download("all-MiniLM-L6-v2")
    assert should_spawn is False
    assert state["status"] == "done"
    assert state.get("cached") is True


def test_begin_download_fresh_spawns(monkeypatch):
    monkeypatch.setattr(es, "is_cached", lambda m: False)
    state, should_spawn = es.begin_download("all-MiniLM-L6-v2")
    assert should_spawn is True
    assert state["status"] == "downloading"


def test_begin_download_duplicate_no_respawn(monkeypatch):
    monkeypatch.setattr(es, "is_cached", lambda m: False)
    es.begin_download("all-MiniLM-L6-v2")
    _state, should_spawn = es.begin_download("all-MiniLM-L6-v2")
    assert should_spawn is False  # 防呆：下載中不重複啟動


# --- perform_download：進度 + additive（絕不 force_download，不覆蓋既有）---

def test_perform_download_reports_progress_additive(monkeypatch):
    monkeypatch.setattr(es, "list_model_files",
                        lambda repo: [("config.json", 100), ("model.safetensors", 300)])
    calls = []

    def fake_dl(**kw):
        calls.append(kw)
        return f"/cache/{kw['filename']}"

    monkeypatch.setattr(es, "hf_hub_download", fake_dl)
    es.perform_download("all-MiniLM-L6-v2")
    st = es.get_download_state("all-MiniLM-L6-v2")
    assert st["status"] == "done"
    assert st["pct"] == 100
    assert len(calls) == 2
    # 防呆②：絕不 force_download（預設 skip 既有檔、不覆蓋）
    assert all(not kw.get("force_download", False) for kw in calls)


def test_perform_download_error_sets_error_state(monkeypatch):
    monkeypatch.setattr(es, "list_model_files", lambda repo: [("config.json", 100)])

    def boom(**kw):
        raise _NET_ERR

    monkeypatch.setattr(es, "hf_hub_download", boom)
    es.perform_download("all-MiniLM-L6-v2")
    st = es.get_download_state("all-MiniLM-L6-v2")
    assert st["status"] == "error"
    assert "network" in st.get("error", "")


# --- 切換防呆③：既有記憶 + 換模型 → 需確認 ---

def test_evaluate_switch_same_model_no_confirmation(monkeypatch):
    monkeypatch.setattr(es, "get_current_embedding_model", lambda: "all-MiniLM-L6-v2")
    monkeypatch.setattr(es, "has_existing_memories", lambda: True)
    ev = es.evaluate_switch("all-MiniLM-L6-v2")
    assert ev["requires_confirmation"] is False


def test_evaluate_switch_diff_model_with_memories_requires_confirmation(monkeypatch):
    monkeypatch.setattr(es, "get_current_embedding_model", lambda: "all-MiniLM-L6-v2")
    monkeypatch.setattr(es, "has_existing_memories", lambda: True)
    ev = es.evaluate_switch("paraphrase-multilingual-MiniLM-L12-v2")
    assert ev["requires_confirmation"] is True
    assert ev["has_memories"] is True


def test_evaluate_switch_diff_model_no_memories_ok(monkeypatch):
    monkeypatch.setattr(es, "get_current_embedding_model", lambda: "all-MiniLM-L6-v2")
    monkeypatch.setattr(es, "has_existing_memories", lambda: False)
    ev = es.evaluate_switch("paraphrase-multilingual-MiniLM-L12-v2")
    assert ev["requires_confirmation"] is False


# --- apply_switch：寫 EMBEDDING_MODEL（SSOT resolve）＋重置單例（免重啟）---

def test_apply_switch_writes_env_and_resets(monkeypatch, tmp_path):
    # set_key 於 .env 不存在時自動建立；以 dotenv get_key 驗證（避免 Path IO 觸發 IO_PRISON）。
    env = tmp_path / ".env"
    monkeypatch.setenv("TOUKA_ENV_FILE", str(env))
    applied: dict = {}
    monkeypatch.setattr(es, "apply_runtime_env", applied.update)
    reset_called = {"n": 0}
    monkeypatch.setattr(es, "_reset_memory_singletons",
                        lambda: reset_called.__setitem__("n", reset_called["n"] + 1))
    es.apply_switch("paraphrase-multilingual-MiniLM-L12-v2")
    assert get_key(str(env), "EMBEDDING_MODEL") == "paraphrase-multilingual-MiniLM-L12-v2"
    assert applied.get("EMBEDDING_MODEL") == "paraphrase-multilingual-MiniLM-L12-v2"
    assert reset_called["n"] == 1  # 重置向量單例 → 新模型免重啟生效
