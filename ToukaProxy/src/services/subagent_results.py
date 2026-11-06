"""耐久 dispatch 結果落盤（in-process 子代理協調提案 Phase 4，需求 3）.

``DispatchCallbackStore`` 是純記憶體 asyncio.Event——重啟即失、正在等的 await
掛不回（雖然結果已算出）。本模組補一層**耐久落盤**：deliver 時把結果寫進
requester 的 personal vault（``dispatch_results/<token>.json``），使
``await_dispatch_result`` 能**耐久優先**——已交付（含跨重啟）即直接重讀、不空等。

分離原則：callback store 維持純記憶體（不耦合 identity/drive）；耐久落在此層，
以呼叫者身分的個人空間為根（區內路徑與原子寫入，ADR-0293）。
落盤是韌性加值，best-effort——失敗不得中斷交付主流程（由呼叫端廣捕）；資料夾被換成
連結或名稱不能用時記警告、不寫，讀取當作沒有結果。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from memory_server.utils.no_follow import LinkRefusedError
from memory_server.utils.zone_paths import ZoneSegmentError

from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from memory_server.utils.zone_paths import ZonePath

    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

_SUBDIR = "dispatch_results"
_REFUSED = "dispatch result skipped: the results folder is a link or the name cannot be used"
# token 為 URL-safe base64（secrets.token_urlsafe）；落檔前白名單化，杜絕路徑穿越。
_TOKEN_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _results_dir(identity: AgentIdentity) -> ZonePath | None:
    """Dispatch 結果目錄（requester 的個人空間下）；解析不到回 None。"""
    drive = resource_manager.agent_drive(identity)
    return None if drive is None else drive.child(_SUBDIR)


def _token_path(identity: AgentIdentity, token: str) -> ZonePath | None:
    directory = _results_dir(identity)
    if directory is None:
        return None
    safe = _TOKEN_SAFE_RE.sub("_", token)[:128]
    return directory.child(f"{safe}.json")


def persist_result(identity: AgentIdentity, token: str, payload: dict[str, Any]) -> None:
    """Requester 身分下把 dispatch 結果耐久落盤（原子寫入）。身分/token 不可解析時靜默跳過。"""
    if not token:
        return
    try:
        path = _token_path(identity, token)
        if path is not None:
            resource_manager.zone_write_json(path, payload)
    except (LinkRefusedError, ZoneSegmentError):
        logger.warning(_REFUSED)


def read_result(identity: AgentIdentity, token: str) -> dict[str, Any] | None:
    """讀回已落盤的 dispatch 結果；不存在／解析失敗回 None（跨重啟可用）。"""
    if not token:
        return None
    try:
        path = _token_path(identity, token)
        data = None if path is None else resource_manager.zone_read_json(path)
    except (LinkRefusedError, ZoneSegmentError):
        logger.warning(_REFUSED)
        return None
    return data if isinstance(data, dict) else None
