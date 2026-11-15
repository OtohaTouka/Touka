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
import re
import threading

from dotenv import set_key
from huggingface_hub import HfApi, hf_hub_download, snapshot_download

from src.config import ENV_FILE_LOCK, apply_runtime_env, resolve_env_file_path

logger = logging.getLogger(__name__)

_DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
_MEMORY_MANAGER_MODULE = "memory_server.services.manager"

# 下載時不抓的格式（ADR-0314）：sentence-transformers 只用 PyTorch 權重，其他執行環境的格式用不到。
UNUSED_FORMAT_FOLDERS: frozenset[str] = frozenset({"onnx", "openvino"})
UNUSED_FORMAT_SUFFIXES: tuple[str, ...] = (".h5", ".ot", ".msgpack")
# 最上層權重檔（含分片）；有 safetensors 時 transformers 優先載入它，.bin 就不用抓。
_SAFETENSORS_WEIGHTS = re.compile(r"model(-\d+-of-\d+)?\.safetensors")
_PYTORCH_WEIGHTS = re.compile(r"pytorch_model(-\d+-of-\d+)?\.bin(\.index\.json)?")

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


def select_download_files(files: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """只留 sentence-transformers 載入時用得到的檔（ADR-0314）。

    排除其他執行環境的格式（ONNX、OpenVINO、TensorFlow、Rust、Flax）；最上層有 safetensors 權重時，
    PyTorch 的 .bin 權重也用不到。斷詞檔（vocab.txt、sentencepiece、tokenizer.json）與模組資料夾
    （如 2_Dense/）內的權重一律保留。e5 一庫約 5.3GB，排除後約 1.1GB。
    """
    names = [name for name, _ in files]
    has_safetensors = any(_SAFETENSORS_WEIGHTS.fullmatch(name) for name in names)

    def wanted(name: str) -> bool:
        folder, slash, _rest = name.partition("/")
        if slash and folder in UNUSED_FORMAT_FOLDERS:
            return False
        if name.endswith(UNUSED_FORMAT_SUFFIXES):
            return False
        return not (has_safetensors and _PYTORCH_WEIGHTS.fullmatch(name))

    return [(name, size) for name, size in files if wanted(name)]


def get_download_state(model_name: str) -> dict:
    """回傳某模型目前的下載狀態（未知則回 idle）。"""
    with _lock:
        return dict(_download_state.get(model_name) or {"status": "idle", "pct": 0})


def _set_state(model_name: str, **fields: object) -> None:
    with _lock:
        _download_state[model_name] = {**_download_state.get(model_name, {}), **fields}


class _DownloadProgress:
    """把下載進度按位元組寫進狀態（ADR-0314）。

    排除用不到的格式後，e5 幾乎只剩一個約 1.1GB 的權重檔，只按檔數算會一直停在 0%。
    實例直接當 `hf_hub_download` 的 `tqdm_class`：它只用到「呼叫建立、context manager、update」，
    一般下載與 Xet 下載都一樣。同一次下載裡的重試沿用同一個物件；伺服器不接受續傳時 `update`
    會收到負數扣回。Xet 的回呼由背景計時呼叫，可能在下載結束後才到，所以結束時先 `stop()`，
    之後的回報不再寫狀態。
    """

    def __init__(self, model_name: str, total: int) -> None:
        self._model = model_name
        self._total = total
        self._finished = 0  # 已完成檔案的位元組
        self._file_size = 0
        self._in_file = 0  # 目前這個檔這次已下載的位元組
        self._stopped = False

    def start_file(self, size: int) -> None:
        self._file_size = size
        self._in_file = 0

    def finish_file(self) -> None:
        self._finished += self._file_size
        self._in_file = 0
        self.publish(self._finished)

    def publish(self, done: int) -> None:
        done = max(0, done)
        with _lock:  # 檢查與寫入同一把鎖：stop() 回傳之後不會再有人寫成下載中
            if self._stopped:
                return
            _download_state[self._model] = {
                **_download_state.get(self._model, {}), "status": "downloading", "downloaded": done,
                "total": self._total, "pct": min(100, int(done / self._total * 100))}

    def stop(self) -> None:
        with _lock:
            self._stopped = True

    def __call__(self, *, initial: int = 0, **_kwargs: object) -> _DownloadProgress:
        """每次開始傳一個檔時建立；initial 是已有的位元組（huggingface_hub 1.21 一律是 0）。"""
        self._in_file = 0
        if initial:
            self.update(initial)
        return self

    def __enter__(self) -> _DownloadProgress:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def update(self, n: int = 1) -> None:
        self._in_file += n
        self.publish(self._finished + min(self._in_file, self._file_size))

    def close(self) -> None:
        return None


def begin_download(model_name: str) -> tuple[dict, bool]:
    """啟動下載（冪等）。回 (狀態, 是否需背景執行)。

    防呆①：已在快取 → 直接標 done、不重下；已在下載/完成 → 不重複啟動。
    上次失敗的不看快取：抓到一半時快取資料夾已經建立，`is_cached` 會當成已下載（ADR-0314）；
    重新下載時已經抓完的檔不會重抓。
    """
    with _lock:
        st = _download_state.get(model_name)
        if st and st["status"] in ("downloading", "done"):
            return dict(st), False
        failed_before = bool(st) and st["status"] == "error"
    if not failed_before and is_cached(model_name):
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
    progress: _DownloadProgress | None = None
    try:
        files = select_download_files(list_model_files(repo_id))
        total = sum(size for _, size in files) or 1
        progress = _DownloadProgress(model_name, total)
        progress.publish(0)
        for filename, size in files:
            progress.start_file(size)
            # additive；未指定 force_download。檔內位元組經 tqdm_class 回報
            hf_hub_download(repo_id=repo_id, filename=filename, tqdm_class=progress)
            progress.finish_file()
        progress.stop()
        _set_state(model_name, status="done", downloaded=total, total=total, pct=100)
        logger.info("[Embedding] Downloaded model %s (%d files)", model_name, len(files))
    except Exception as e:  # noqa: BLE001 — 背景任務：任何失敗都須落狀態供前端顯示，不得逸出
        logger.exception("[Embedding] Download failed for %s", model_name)
        if progress is not None:
            progress.stop()
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
    with ENV_FILE_LOCK:  # 在記憶工作者上寫，和主迴圈上寫 .env 的地方互斥（ADR-0288）
        set_key(str(env_path), "EMBEDDING_MODEL", target)
    apply_runtime_env({"EMBEDDING_MODEL": target})
    _reset_memory_singletons()


def switch_model(target_model: str, *, force: bool) -> dict:
    """偵測，可以就套用：放在同一個記憶工作裡，中間不會插進用舊模型的記憶操作（ADR-0288）。

    已有記憶且換不同模型時，未 force 只回需確認、不寫入。
    """
    evaluation = evaluate_switch(target_model)
    if evaluation["requires_confirmation"] and not force:
        return {"status": "confirmation_required", **evaluation}
    apply_switch(target_model)
    return {"status": "ok", **evaluation}
