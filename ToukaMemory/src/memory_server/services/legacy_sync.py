"""遺產紀錄同步服務 (Legacy Sync Service).

本模組負責把對話紀錄歸檔進 SQLite 舊對話庫：滑動視窗剪下的對話、前端「切分存入舊對話」與
使用者放進 legacy 目錄的 JSON 檔都經 ``archive_payload`` 直接寫庫。``legacy/`` 受
``is_protected_path`` 保護，系統不在裡面寫檔或搬檔；同步以 (檔名, 內容 SHA-256) 記已處理。
具備啟發式提取引擎，支援解析 Open WebUI 等多種不同的對話結構。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from memory_server.config_utils import get_current_epoch, get_resource_proxy
from memory_server.services.legacy_store import ArchiveOutcome, LegacyStore
from memory_server.utils.legacy_time import to_epoch_ms

if TYPE_CHECKING:
    from pathlib import Path

    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)


def _stable_message_id(session_id: str, index: int, content: Any) -> str:
    """沒有 id 的訊息：由所屬 session、位置與內容決定 id."""
    key = json.dumps([session_id, index, content], ensure_ascii=False, default=str)
    return f"msg_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


class LegacySyncService:
    """遺產紀錄同步與遷移管理員.

    負責處理 JSON 檔案到 SQLite 資料庫的單向或雙向同步邏輯。

    Attributes:
        manager (MemoryManager): 關聯的記憶管理器實例。
        stores (dict[str, LegacyStore]): 已快取的各代理人資料庫實例。
    """

    def __init__(self, manager: Any) -> None:
        """初始化同步服務.

        Args:
            manager (MemoryManager): 記憶管理總管實例。
        """
        self.manager = manager
        self.stores: dict[str, LegacyStore] = {}

    def _legacy_dir(self, identity: AgentIdentity) -> Path:
        """經資源代理解析 Legacy 目錄；只解析路徑，不建立目錄."""
        return self.manager.get_legacy_dir(identity, resource_proxy=get_resource_proxy())

    def get_store(self, identity: AgentIdentity) -> LegacyStore:
        """獲取或建立代理人的專屬 Legacy 資料庫實例 (SSOT).

        Args:
            identity (AgentIdentity): 代理人身分識別。

        Returns:
            LegacyStore: 資料庫存取實例。
        """
        agent_id = identity.agent_id
        if agent_id not in self.stores:
            db_path = self._legacy_dir(identity) / "legacy_history.db"
            self.stores[agent_id] = LegacyStore(str(db_path))
        return self.stores[agent_id]

    def archive_payload(
        self,
        identity: AgentIdentity,
        data: Any,
        filename: str,
        source: tuple[str, str] | None = None,
    ) -> int:
        """把一份對話資料（各種匯出格式）直接歸檔進舊對話庫，回確認在庫的訊息數.

        滑動視窗剪裁、前端「切分存入舊對話」與手動同步共用；不寫任何檔案。``filename`` 只是
        資料庫裡的標籤（不當路徑）。已經在庫的訊息也算確認在庫，同 id 但內容不同的另存成版本、
        也算在內（舊審查報告乙-1）。失敗往上拋（交易已 rollback），由呼叫端決定：
        剪裁不剪、同步記錯。
        """
        return self._archive(identity, data, filename, source).confirmed

    def _archive(
        self,
        identity: AgentIdentity,
        data: Any,
        filename: str,
        source: tuple[str, str] | None,
    ) -> ArchiveOutcome:
        default_id = filename.removesuffix(".json")
        messages = self._extract_messages(data, default_id)
        if not messages and source is None:
            return ArchiveOutcome(confirmed=0, versions=0)
        now_ms = int(get_current_epoch() * 1000)
        sessions: dict[str, dict[str, Any]] = {}
        for msg in messages:
            s_id = msg["session_id"]
            if s_id not in sessions:
                sessions[s_id] = {
                    "id": s_id,
                    "filename": filename,
                    "title": self._session_title(data, s_id, default_id),
                    "created_at": msg.get("timestamp") or now_ms,
                    "updated_at": now_ms,
                }
        get_resource_proxy().make_dir(str(self._legacy_dir(identity)))
        return self.get_store(identity).archive(
            identity, list(sessions.values()), messages, source
        )

    @staticmethod
    def _session_title(data: Any, session_id: str, default: str) -> str:
        """啟發式標題：清單找同 id 的 session，單一物件取自己的 title／name."""
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id") == session_id:
                    return item.get("title", item.get("name", default))
            return default
        if isinstance(data, dict):
            return data.get("title", data.get("name", default))
        return default

    def _json_files(self, identity: AgentIdentity) -> list[Path]:
        """Legacy 目錄最上層的 JSON 檔；目錄不存在回空清單（不建立）."""
        agent_dir = self._legacy_dir(identity)
        if not get_resource_proxy().exists(str(agent_dir)):
            return []
        return sorted(f for f in agent_dir.iterdir() if f.suffix == ".json")

    @staticmethod
    def _read_with_digest(file_path: Path) -> tuple[str, str]:
        content = get_resource_proxy().read_file_text(str(file_path))
        return content, hashlib.sha256(content.encode("utf-8")).hexdigest()

    def sync_all_json_to_db(self, identity: AgentIdentity) -> dict[str, Any]:
        """把 legacy 目錄裡還沒處理過的 JSON 檔歸檔進舊對話庫.

        不搬檔：以 (檔名, 內容 SHA-256) 記已處理，重跑跳過；歸檔失敗的檔不留紀錄、計入 errors，
        下次重跑重做。同 id 但內容不同、另存成版本的筆數記在 conflicts（舊審查報告乙-1）。

        Returns:
            dict[str, Any]: synced／skipped／errors／conflicts 與資料庫統計。
        """
        store = self.get_store(identity)
        done = store.processed_keys()
        synced = skipped = errors = conflicts = 0
        for file_path in self._json_files(identity):
            f_name = file_path.name
            try:
                content, digest = self._read_with_digest(file_path)
                if (f_name, digest) in done:
                    skipped += 1
                    continue
                outcome = self._archive(
                    identity, json.loads(content), f_name, source=(f_name, digest)
                )
            except Exception:
                logger.exception("❌ [LegacySync] Failed to sync %s", f_name)
                errors += 1
                continue
            conflicts += outcome.versions
            if outcome.confirmed:
                synced += 1
            else:
                logger.warning("No valid messages found in %s", f_name)

        return {
            "synced": synced,
            "skipped": skipped,
            "errors": errors,
            "conflicts": conflicts,
            "total_in_db": store.get_stats(),
        }

    def file_status(self, identity: AgentIdentity) -> dict[str, int]:
        """設定頁統計（只讀）：已處理紀錄數、目錄裡還沒處理（或讀不到）的 JSON 檔數."""
        done = self.get_store(identity).processed_keys()
        pending = 0
        for file_path in self._json_files(identity):
            try:
                _, digest = self._read_with_digest(file_path)
            except (OSError, ValueError):  # 讀不到或不是 UTF-8：算待處理，不讓統計失敗
                pending += 1
                continue
            pending += (file_path.name, digest) not in done
        return {"processed": len(done), "pending": pending}

    def _extract_messages(self, data: Any, file_session_id: str) -> list[dict[str, Any]]:
        """啟發式提取訊息引擎.

        將各種不同來源 (如 Open WebUI, 舊版 Touka) 的 JSON 結構統一提取為扁平的訊息清單。

        Args:
            data (Any): JSON 原始物件。
            file_session_id (str): 預設的會話識別碼。

        Returns:
            list[dict[str, Any]]: 歸一化後的訊息列表。
        """
        sessions_to_process = []
        if isinstance(data, list):
            sessions_to_process = data
        elif isinstance(data, dict):
            # 支援多種常見格式
            if any(k in data for k in ["chat", "history", "messages"]):
                sessions_to_process = [data]
            else:
                sessions_to_process = [data]

        all_messages = []
        now_ms = int(get_current_epoch() * 1000)

        for session_item in sessions_to_process:
            if not isinstance(session_item, dict):
                continue
            
            s_id = session_item.get("id") or file_session_id
            
            # 從多種可能的路徑提取訊息列表
            msgs: Any = []
            if "chat" in session_item:
                chat_data = session_item["chat"]
                if isinstance(chat_data, dict):
                    msgs = chat_data.get("messages", [])
                    if not msgs and "history" in chat_data:
                        hist = chat_data["history"]
                        if isinstance(hist, dict) and "messages" in hist:
                            msgs = list(hist["messages"].values())
                    elif not msgs and "chat" in chat_data:
                        msgs = chat_data["chat"].get("messages", [])
            elif "history" in session_item:
                hist = session_item["history"]
                if isinstance(hist, dict) and "messages" in hist:
                    msgs = list(hist["messages"].values())
            elif "messages" in session_item:
                msgs = session_item["messages"]
            elif "content" in session_item and "role" in session_item:
                msgs = [session_item]

            # 轉為列表格式
            if isinstance(msgs, dict):
                msgs = list(msgs.values())
            if not isinstance(msgs, list):
                continue

            for index, msg in enumerate(msgs):
                if not isinstance(msg, dict) or "content" not in msg:
                    continue

                # 沒有 id 的用決定性 id（同一份內容重跑不多一份）
                m_id = msg.get("id") or _stable_message_id(s_id, index, msg["content"])
                
                all_messages.append({
                    **msg,
                    "id": m_id,
                    "session_id": s_id,
                    # 一律毫秒：秒、數字字串、帶時區 ISO 都換算；都沒有才用同步當下
                    "timestamp": to_epoch_ms(msg.get("timestamp"))
                    or to_epoch_ms(msg.get("createdAt"))
                    or now_ms,
                })
            
        return all_messages

    def export_session_to_json(self, identity: AgentIdentity, session_id: str) -> dict[str, Any]:  # noqa: ARG002
        """從資料庫還原出原始 JSON 結構 (反向匯出)."""
        # 未來實作完整對話樹還原邏輯
        return {}
