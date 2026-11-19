"""耐久 dispatch 結果落盤（in-process 子代理協調提案 Phase 4，需求 3；位置 ADR-0324）.

``DispatchCallbackStore`` 是純記憶體 asyncio.Event——重啟即失、正在等的 await
掛不回（雖然結果已算出）。本模組補一層**耐久落盤**：deliver 時把結果寫一份，使
``await_dispatch_result`` 能**耐久優先**——已交付（含跨重啟）即直接重讀、不空等。

分離原則：callback store 維持純記憶體（不耦合 identity/drive）；耐久落在此層。
落盤是韌性加值，best-effort——失敗不得中斷交付主流程（由呼叫端廣捕）。

位置（ADR-0324）：data 底下的系統暫存 ``temp/dispatch_results``，不在任何代理人資料夾裡。以前放在
主代理自己的個人空間 ``dispatch_results/<憑證>.json``，主代理（或被注入誘導）用檔案動詞就能先寫一份
假結果；舊位置不再讀。檔名是「代理人編號（casefold）＋憑證」的 SHA-256，磁碟上看不到憑證、別的
代理人拿同一個憑證也讀不到。超過 ``RESULT_RETENTION_SECONDS`` 的檔在啟動時、每次存與讀時清掉。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.services.resource_manager import resource_manager
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

_SUBDIR = "dispatch_results"
# 只為「重啟後還拿得回結果」：站內憑證在記憶體最多約 40 分鐘，7 天涵蓋隔夜與週末（ADR-0324）
RESULT_RETENTION_SECONDS = 7 * 24 * 3600
_NAME_SEPARATOR = "\0"
_NS_PER_SECOND = 1_000_000_000


def _results_dir() -> Path:
    """結果資料夾（data/temp 底下，代理人的檔案動詞與終端沙盒都碰不到）。"""
    return Path(resource_manager.get_temp_path(_SUBDIR))


def _result_path(identity: AgentIdentity, token: str) -> Path:
    key = f"{identity.agent_id.casefold()}{_NAME_SEPARATOR}{token}"
    return _results_dir() / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"


def prune_expired_results() -> int:
    """刪掉超過保留期限的結果檔，回刪掉幾個；資料夾不存在時什麼都不做（不建）。

    單一檔刪不掉只記警告、繼續下一個；列不出資料夾時往外丟，由呼叫端決定（啟動時只記 log）。
    """
    cutoff_ns = int((get_current_epoch() - RESULT_RETENTION_SECONDS) * _NS_PER_SECOND)
    directory = _results_dir()
    removed = 0
    for rel, _size, mtime_ns in resource_manager.stat_files(directory):
        if "/" in rel or mtime_ns >= cutoff_ns:
            continue
        try:
            resource_manager.remove_file(directory / rel, missing_ok=True)
        except OSError:
            logger.warning("expired dispatch result could not be removed: %s", rel)
        else:
            removed += 1
    return removed


def _prune_logged() -> None:
    """存、讀時順手清；清不了只記警告，不影響這次的存與讀。"""
    try:
        prune_expired_results()
    except OSError:
        logger.warning("expired dispatch results were not pruned", exc_info=True)


def persist_result(identity: AgentIdentity, token: str, payload: dict[str, Any]) -> None:
    """Requester 身分下把 dispatch 結果耐久落盤（原子寫入）。token 為空時跳過。"""
    if not token:
        return
    _prune_logged()
    resource_manager.write_json(_result_path(identity, token), payload)


def read_result(identity: AgentIdentity, token: str) -> dict[str, Any] | None:
    """讀回已落盤的 dispatch 結果；不存在／解析失敗回 None（跨重啟可用）。"""
    if not token:
        return None
    _prune_logged()
    data = resource_manager.read_json(_result_path(identity, token))
    return data if isinstance(data, dict) else None
