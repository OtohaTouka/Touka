"""嵌入模型下載與切換服務（bug①）。

三個動作嚴格分離，杜絕誤觸資料：
  - **下載**：抓模型檔到 HF 快取（per-model 目錄、絕不刪既有、不 re-embed）→ 天生安全。
  - **切換**：寫 EMBEDDING_MODEL；偵測到既有記憶且換不同模型時需使用者確認
    （既有向量將失效至手動重新嵌入前；本模組**絕不自動 re-embed、不直接寫事實**；重置單例後
    下一次取用事實庫時，若結構仍落後會由 FactualStore 自己再試一次升級（含備份））。
  - **重新嵌入**：獨立離線維護操作（`reembed_memory.py`），本模組永不觸發。

下載採「背景執行 + 輪詢狀態」；進度以逐檔累積位元組回報。跨專案記憶系統以 importlib
動態載入（整合模式；未載入時防禦式降級為「無記憶」）。
"""
from __future__ import annotations

import importlib
import logging
import threading

from dotenv import set_key
from huggingface_hub import HfApi, hf_hub_download, snapshot_download

from src.config import apply_runtime_env, resolve_env_file_path

logger = logging.getLogger(__name__)

_DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
_MEMORY_MANAGER_MODULE = "memory_server.services.manager"

# 下載狀態（per-model）：{model: {status, downloaded, total, pct, cached?, error?}}。
_download_state: dict[str, dict] = {}
_lock = threading.Lock()


def reset_download_state() -> None:
    """清空下載狀態（測試隔離用）。"""
    with _lock:
        _download_state.clear()


def resolve_repo_id(model_name: str) -> str:
    """SentenceTransformer 短名 → HF repo id（無 '/' 則補 sentence-transformers/ 前綴）。"""
    name = (model_name or "").strip()
    return name if "/" in name else f"sentence-transformers/{name}"


def is_cached(model_name: str) -> bool:
    """該模型是否已完整存在於本機 HF 快取（local_files_only 探測，不觸發任何下載）。"""
    try:
        snapshot_download(repo_id=resolve_repo_id(model_name), local_files_only=True)
    except Exception:
        return False
    else:
        return True


def list_model_files(repo_id: str) -> list[tuple[str, int]]:
    """回傳 repo 內各檔案 (檔名, 位元組)；用於逐檔累積下載進度。"""
    info = HfApi().model_info(repo_id, files_metadata=True)
    return [(s.rfilename, int(getattr(s, "size", 0) or 0)) for s in (info.siblings or [])]


def get_download_state(model_name: str) -> dict:
    """回傳某模型目前的下載狀態（未知則回 idle）。"""
    with _lock:
        return dict(_download_state.get(model_name) or {"status": "idle", "pct": 0})


def _set_state(model_name: str, **fields: object) -> None:
    with _lock:
        _download_state[model_name] = {**_download_state.get(model_name, {}), **fields}


def begin_download(model_name: str) -> tuple[dict, bool]:
    """啟動下載（冪等）。回 (狀態, 是否需背景執行)。

    防呆①：已在快取 → 直接標 done、不重下；已在下載/完成 → 不重複啟動。
    """
    with _lock:
        st = _download_state.get(model_name)
        if st and st["status"] in ("downloading", "done"):
            return dict(st), False
    if is_cached(model_name):
        state = {"status": "done", "downloaded": 0, "total": 0, "pct": 100, "cached": True}
        _set_state(model_name, **state)
        return state, False
    state = {"status": "downloading", "downloaded": 0, "total": 0, "pct": 0}
    _set_state(model_name, **state)
    return state, True


def perform_download(model_name: str) -> None:
    """（阻塞）逐檔下載並更新進度；失敗記錄於狀態。

    防呆②：一律不 force_download → hf_hub_download 跳過既有檔、**絕不覆蓋既有模型**。
    """
    repo_id = resolve_repo_id(model_name)
    try:
        files = list_model_files(repo_id)
        total = sum(size for _, size in files) or 1
        _set_state(model_name, status="downloading", downloaded=0, total=total, pct=0)
        done = 0
        for filename, size in files:
            hf_hub_download(repo_id=repo_id, filename=filename)  # additive；未指定 force_download
            done += size
            _set_state(model_name, status="downloading", downloaded=done, total=total,
                       pct=min(100, int(done / total * 100)))
        _set_state(model_name, status="done", downloaded=total, total=total, pct=100)
        logger.info("[Embedding] Downloaded model %s (%d files)", model_name, len(files))
    except Exception as e:  # noqa: BLE001 — 背景任務：任何失敗都須落狀態供前端顯示，不得逸出
        logger.exception("[Embedding] Download failed for %s", model_name)
        _set_state(model_name, status="error", error=str(e))


# --- 切換防呆 ---

def get_current_embedding_model() -> str:
    """目前生效的嵌入模型名（經 ToukaMemory config SSOT；記憶系統未載入則回預設）。"""
    try:
        cfg = importlib.import_module("memory_server.config_utils")
        return cfg.get_embedding_model_name()
    except Exception:  # noqa: BLE001 — 記憶系統未載入時回預設
        return _DEFAULT_EMBEDDING_MODEL


def has_existing_memories() -> bool:
    """是否已有任何 agent 的事實記憶（防呆③偵測）。

    以無損 FactualStore 存在性為準（limit=1 探測）；記憶系統未載入 → 回 False（視為全新安裝）。
    記憶庫讀不到或查詢失敗 → 回 True：當作有記憶，換模型時一定要求確認。
    """
    try:
        manager_mod = importlib.import_module(_MEMORY_MANAGER_MODULE)
    except Exception:  # noqa: BLE001 — 記憶系統未載入時防禦式降級
        logger.debug("[Embedding] memory system unavailable; assuming no memories.", exc_info=True)
        return False
    try:
        factual = manager_mod.MemoryManager().get_factual_store()
        rows = factual.query_all_facts(include_archived=True, limit=1)
    except Exception:  # 讀不到不能當成沒有記憶
        logger.warning("[Embedding] memory presence check failed; assuming memories exist.")
        return True
    return bool(rows)


def evaluate_switch(target_model: str) -> dict:
    """評估切換嵌入模型是否需要使用者確認（既有記憶 + 換不同模型 → 需確認）。"""
    current = get_current_embedding_model()
    target = (target_model or "").strip()
    has_mem = has_existing_memories()
    return {
        "current_model": current,
        "target_model": target,
        "has_memories": has_mem,
        "requires_confirmation": (target != current) and has_mem,
    }


def _reset_memory_singletons() -> None:
    """重置記憶單例，使新嵌入模型免重啟即生效（記憶系統未載入則略過）。"""
    try:
        manager_mod = importlib.import_module(_MEMORY_MANAGER_MODULE)
        manager_mod.MemoryManager.reset_singletons()
    except Exception:  # noqa: BLE001 — 記憶系統未載入時略過重置
        logger.debug("[Embedding] singleton reset skipped (memory unavailable).", exc_info=True)


def apply_switch(target_model: str) -> None:
    """寫入 EMBEDDING_MODEL（經 resolve_env_file_path SSOT）、即時套用並重置向量單例。

    **絕不 re-embed、不直接寫事實**（重置後下一次取用事實庫時可能再試一次結構升級）；
    既有向量相容性由呼叫端確認流程把關。
    """
    target = (target_model or "").strip()
    if not target:
        msg = "target_model must not be empty"
        raise ValueError(msg)  # noqa: TRY003
    env_path = resolve_env_file_path()
    set_key(str(env_path), "EMBEDDING_MODEL", target)
    apply_runtime_env({"EMBEDDING_MODEL": target})
    _reset_memory_singletons()
