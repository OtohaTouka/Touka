"""耐久 dispatch 結果落盤（in-process 子代理協調提案 Phase 4，需求 3）.

``DispatchCallbackStore`` 是純記憶體 asyncio.Event——重啟即失、正在等的 await
掛不回（雖然結果已算出）。本模組補一層**耐久落盤**：deliver 時把結果寫進
requester 的 personal vault（``dispatch_results/<token>.json``），使
``await_dispatch_result`` 能**耐久優先**——已交付（含跨重啟）即直接重讀、不空等。

分離原則：callback store 維持純記憶體（不耦合 identity/drive）；耐久落在此層，
以呼叫者身分的 vault 為根（沿用既有 ``resource_manager.get_vault_path``／原子寫入）。
落盤是韌性加值，best-effort——失敗不得中斷交付主流程（由呼叫端廣捕）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

_SUBDIR = "dispatch_results"
# token 為 URL-safe base64（secrets.token_urlsafe）；落檔前白名單化，杜絕路徑穿越。
_TOKEN_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _results_dir(identity: AgentIdentity) -> Path | None:
    """Dispatch 結果目錄（requester 的 personal vault 下）；解析不到回 None。"""
    base = resource_manager.get_vault_path("personal", identity)
    if not base:
        return None
    return Path(base) / _SUBDIR


def _token_path(identity: AgentIdentity, token: str) -> Path | None:
    directory = _results_dir(identity)
    if directory is None:
        return None
    safe = _TOKEN_SAFE_RE.sub("_", token)[:128]
    return directory / f"{safe}.json"


def persist_result(identity: AgentIdentity, token: str, payload: dict[str, Any]) -> None:
    """Requester 身分下把 dispatch 結果耐久落盤（原子寫入）。身分/token 不可解析時靜默跳過。"""
    path = _token_path(identity, token)
    if path is None or not token:
        return
    resource_manager.make_dir(str(path.parent))
    resource_manager.write_json(str(path), payload)


def read_result(identity: AgentIdentity, token: str) -> dict[str, Any] | None:
    """讀回已落盤的 dispatch 結果；不存在／解析失敗回 None（跨重啟可用）。"""
    path = _token_path(identity, token)
    if path is None or not token or not path.exists():
        return None
    data = resource_manager.read_json(str(path))
    return data if isinstance(data, dict) else None
