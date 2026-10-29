"""ADR-0081 游標續傳可恢復串流 — 進行中互動串流的 per-session 緩衝（SSOT）。

職責（SRP）：
- 對每個進行中的 UI 對話串流維護一份 in-memory 緩衝：原樣 SSE 行（seq = list index）、
  狀態機（active → done/error/cancelled）、producer task 句柄。
- 訂閱者（HTTP 回應或 resume 端點）以 ``subscribe(starting_after)`` 重播既有 chunk
  並以 ``asyncio.Event`` 跟流，與 producer（生成）完全解耦——client 斷線只
  取消訂閱者，生成續跑（Vercel resumable-stream 藍圖的 Touka 對應）。
- 同 session 開新串流時取消舊串流（防接到過期串流的競態，對應 activeStreamId 守則）。
- 記下還沒開始串流的 UI 請求是誰送的（UserPromptSubmit hook 執行中、組上下文中），讓停止端點
  在還沒有串流、無痕查不到對話時也認得請求者；停止只標這個人在等的那幾則請求，之後的請求
  清不掉（ADR-0292）。

誠實限制：in-memory——server 重啟＝生成本就隨進程死亡，緩衝同滅；完成後保留
``STREAM_BUFFER_TTL_SECONDS``，過期只能重新讀取已確認持久化的歷史；未保存草稿
不保證在關閉視窗或 server 重啟後恢復。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.session_persistence import (
    SaveResult,
    SkippedResult,
    StreamStartedEvent,
    StreamTerminalEvent,
    UiPersistence,
    UnknownResult,
)
from src.services.interjection_queue import InterjectionQueue, interjection_queue
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)

# 完成後緩衝保留秒數（與 OpenAI background mode 同規格，使用者已確認 10 分鐘）
STREAM_BUFFER_TTL_SECONDS = 600
# 單一串流 chunk 上限（記憶體保險；正常 ReAct 多輪遠低於此，超限終結為 error）
STREAM_BUFFER_MAX_CHUNKS = 50_000
# active 串流最長壽命（孤兒 producer 保險）
STREAM_ACTIVE_MAX_SECONDS = 3600
# 訂閱者無新事件時的 SSE 保活間隔（照 terminal.py /stream 範本）
SUBSCRIBE_KEEPALIVE_SECONDS = 30


@dataclass
class StreamEntry:
    """單一進行中串流的緩衝狀態。"""

    session_id: str
    stream_id: str
    started_at: float
    chunks: list[str] = field(default_factory=list)
    status: str = "active"  # active | done | error | cancelled
    finished_at: float | None = None
    task: asyncio.Task[Any] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    owner: AgentIdentity | None = None
    admission: UiPersistence | None = None
    # 這一輪的 hook 共用欄位（`hooks.events.base_payload`：對話、代理人等；
    # 插話的 hook 用，ADR-0292）
    hook_fields: Mapping[str, Any] = field(default_factory=dict)
    started: StreamStartedEvent | None = None
    terminal: StreamTerminalEvent | None = None


@dataclass(eq=False)
class PendingRequest:
    """一則還沒開始串流的 UI 請求（ADR-0292）。``stopped`` 只由停止端點設，不隨對話的旗標清掉。"""

    requester: AgentIdentity
    stopped: bool = False


class StreamOwnershipError(ValueError):
    """UI entry 必須由已驗證 requester 建立。"""

    def __init__(self) -> None:
        super().__init__("Authenticated UI stream owner required")


class StreamBufferService:
    """進行中互動串流緩衝的唯一事實來源。"""

    def __init__(self, interjections: InterjectionQueue = interjection_queue) -> None:
        self._streams: dict[str, StreamEntry] = {}
        # 還沒開始串流的 UI 請求：session → 每則請求一筆（以物件本身區分）
        self._pending: dict[str, set[PendingRequest]] = {}
        # 插話佇列（ADR-0249）：串流結束或同 session 開新串流時清空
        self._interjections = interjections

    # ------------------------------------------------------------------
    # 生命週期
    # ------------------------------------------------------------------

    async def create(
        self,
        session_id: str,
        *,
        identity: AgentIdentity | None = None,
        admission: UiPersistence | None = None,
        hook_fields: Mapping[str, Any] | None = None,
    ) -> StreamEntry:
        """單 event-loop 無 await 的替換；不留舊／新 entry 交錯建立窗口。"""
        if admission is not None and identity is None:
            raise StreamOwnershipError
        existing = self._streams.get(session_id)
        if (
            existing
            and existing.admission is not None
            and (
                admission is None
                or identity is None
                or existing.owner is None
                or existing.owner.agent_id != identity.agent_id
                or existing.owner.is_human != identity.is_human
            )
        ):
            raise StreamOwnershipError
        self._sweep()
        existing = self._streams.get(session_id)
        if existing and existing.status == "active":
            logger.info(
                "🔁 [StreamBuffer] Replacing active stream for %s (old=%s)",
                session_id,
                existing.stream_id,
            )
            self._terminate(existing, "cancelled")

        entry = StreamEntry(
            session_id=session_id,
            stream_id=uuid.uuid4().hex,
            started_at=float(get_current_epoch()),
            owner=identity,
            admission=admission,
            hook_fields=dict(hook_fields or {}),
        )
        if admission is not None:
            entry.started = StreamStartedEvent(
                session_id=session_id,
                stream_id=entry.stream_id,
                user_parent_id=admission.user_parent_id,
                assistant_node_id=admission.assistant_node_id,
            )
            entry.chunks.append(self._frame(entry.started))
        self._interjections.clear(session_id)
        self._streams[session_id] = entry
        return entry

    @staticmethod
    def _frame(event: StreamStartedEvent | StreamTerminalEvent) -> str:
        return f"data: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"

    def _entry_for(self, session_id: str, stream_id: str | None) -> StreamEntry | None:
        entry = self._streams.get(session_id)
        if entry is None or (entry.admission is not None and stream_id != entry.stream_id):
            return None
        if stream_id is not None and stream_id != entry.stream_id:
            return None
        return entry

    def attach_task(
        self, session_id: str, task: asyncio.Task[Any], *, stream_id: str | None = None
    ) -> None:
        """登記 producer task 句柄（供 cancel／取代時連坐取消）。"""
        entry = self._entry_for(session_id, stream_id)
        if entry and entry.status == "active" and entry.task is None:
            entry.task = task
        else:
            task.cancel()

    async def append(self, session_id: str, chunk: str, *, stream_id: str | None = None) -> bool:
        """追加一個 SSE chunk（seq = index）。非 active 狀態為 no-op。"""
        entry = self._entry_for(session_id, stream_id)
        if not entry or entry.status != "active":
            return False
        reserved = 1 if entry.admission is not None else 0
        if len(entry.chunks) >= STREAM_BUFFER_MAX_CHUNKS - reserved:
            logger.error(
                "🛑 [StreamBuffer] %s exceeded MAX_CHUNKS=%d; finishing as error.",
                session_id,
                STREAM_BUFFER_MAX_CHUNKS,
            )
            self._terminate(entry, "error")
            return False
        entry.chunks.append(chunk)
        entry.changed.set()
        return True

    async def finish(
        self,
        session_id: str,
        status: str = "done",
        *,
        stream_id: str | None = None,
        persistence: SaveResult | None = None,
    ) -> None:
        """標記終態並喚醒所有訂閱者。已是終態則 no-op（冪等）。"""
        entry = self._entry_for(session_id, stream_id)
        if not entry or entry.status != "active":
            return
        self._complete(entry, status, persistence)

    async def cancel(self, session_id: str, *, stream_id: str | None = None) -> bool:
        """取消進行中串流：取消 producer task、標記 cancelled、喚醒訂閱者。"""
        entry = self._entry_for(session_id, stream_id)
        if not entry or entry.status != "active":
            return False
        self._terminate(entry, "cancelled")
        return True

    def _terminate(self, entry: StreamEntry, status: str) -> None:
        if entry.task and not entry.task.done():
            entry.task.cancel()
        self._complete(entry, status)

    def _complete(
        self, entry: StreamEntry, status: str, persistence: SaveResult | None = None
    ) -> None:
        """單 event-loop 的無 await 原子轉移；terminal frame/status/通知一起完成。"""
        if entry.status != "active":
            return
        if entry.started is not None:
            if persistence is None:
                persistence = (
                    SkippedResult(status="skipped", reason="incognito")
                    if entry.admission.mode == "incognito"
                    else UnknownResult(
                        status="unknown",
                        error_code=ToukaException(ErrorCode.SESSION_SAVE_FAILED).error_id,
                    )
                )
            entry.terminal = StreamTerminalEvent(
                **entry.started.model_dump(exclude={"type"}),
                generation="completed"
                if status == "done"
                else "cancelled"
                if status == "cancelled"
                else "error",
                persistence=persistence,
            )
            entry.chunks.append(self._frame(entry.terminal))
        entry.status = status
        entry.finished_at = float(get_current_epoch())
        self._interjections.clear(entry.session_id, entry.stream_id)
        entry.changed.set()

    def is_owned_by(self, session_id: str, identity: AgentIdentity) -> bool:
        """UI buffer 私密內容只可供原 requester，不以 sid/stream_id 代替認證。"""
        self._sweep()
        entry = self._streams.get(session_id)
        return bool(
            entry and entry.admission and entry.owner and _same_requester(entry.owner, identity)
        )

    @contextmanager
    def pending_request(
        self, session_id: str, requester: AgentIdentity
    ) -> Iterator[PendingRequest]:
        """登記一則還沒開始串流的 UI 請求，離開時移除（只移除自己這一筆）。"""
        request = PendingRequest(requester)
        self._pending.setdefault(session_id, set()).add(request)
        try:
            yield request
        finally:
            waiting = self._pending.get(session_id, set())
            waiting.discard(request)
            if not waiting:
                self._pending.pop(session_id, None)

    def has_pending_request(self, session_id: str, identity: AgentIdentity) -> bool:
        """``identity`` 在這個對話有還沒開始串流的請求，而且沒有進行中的串流。

        有進行中的串流時回 False：停止要指名那一代（不能用沒指名的停止停到它）。
        """
        self._sweep()
        entry = self._streams.get(session_id)
        if entry is not None and entry.status == "active":
            return False
        return any(
            _same_requester(request.requester, identity)
            for request in self._pending.get(session_id, set())
        )

    def stop_pending_requests(self, session_id: str, identity: AgentIdentity) -> bool:
        """把 ``identity`` 在這個對話還在等的請求標成已停止；條件同 ``has_pending_request``。"""
        if not self.has_pending_request(session_id, identity):
            return False
        for request in self._pending[session_id]:
            if _same_requester(request.requester, identity):
                request.stopped = True
        return True

    def persistence_mode(
        self, session_id: str, stream_id: str
    ) -> Literal["persistent", "incognito"] | None:
        """僅查指定代的模式；不把 admission revision 或 requester 暴露到公開 state。"""
        entry = self._entry_for(session_id, stream_id)
        return entry.admission.mode if entry and entry.admission else None

    def hook_fields_of(self, session_id: str, stream_id: str) -> dict[str, Any]:
        """指定代的串流建立時記下的 hook 共用欄位；沒有這一代時是空的。"""
        entry = self._entry_for(session_id, stream_id)
        return dict(entry.hook_fields) if entry else {}

    def retire_persistent(self, session_id: str, stream_id: str) -> bool:
        """已確認刪除後退休原代；無 await，不得清除後來建立的新代或無痕 entry。"""
        entry = self._entry_for(session_id, stream_id)
        if not entry or not entry.admission or entry.admission.mode != "persistent":
            return False
        self._terminate(entry, "cancelled")
        del self._streams[session_id]
        return True

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------

    def _state_of(self, entry: StreamEntry) -> dict[str, Any]:
        return {
            "session_id": entry.session_id,
            "stream_id": entry.stream_id,
            "status": entry.status,
            "last_seq": len(entry.chunks) - 1,
            "started_at": entry.started_at,
            **(
                {
                    "started": entry.started.model_dump(mode="json"),
                    "terminal": entry.terminal.model_dump(mode="json") if entry.terminal else None,
                }
                if entry.started is not None
                else {}
            ),
        }

    def get_state(self, session_id: str) -> dict[str, Any] | None:
        """回傳串流狀態摘要；不存在（或已清掃）回 None。"""
        self._sweep()
        entry = self._streams.get(session_id)
        return self._state_of(entry) if entry else None

    def list_active(self) -> list[dict[str, Any]]:
        """列出所有 active 串流的狀態摘要。"""
        self._sweep()
        return [self._state_of(e) for e in self._streams.values() if e.status == "active"]

    # ------------------------------------------------------------------
    # 訂閱（重播 + 跟流）
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        session_id: str,
        starting_after: int = -1,
        keepalive_seconds: float = SUBSCRIBE_KEEPALIVE_SECONDS,
        *,
        stream_id: str | None = None,
    ) -> AsyncGenerator[str]:
        """重播 ``chunks[starting_after+1:]`` 後跟流至終態。

        client 斷線取消的是本訂閱者協程，不影響 producer 與其他訂閱者。
        無新事件每 ``keepalive_seconds`` 發 ``: heartbeat`` 保活。
        """
        entry = self._entry_for(session_id, stream_id)
        if not entry:
            return
        idx = max(starting_after + 1, 0)
        while True:
            while idx < len(entry.chunks):
                yield entry.chunks[idx]
                idx += 1
            if entry.status != "active":
                return
            # clear/recheck 間沒有 await；另一 waiter 清旗標不會撤回已喚醒的 Future。
            entry.changed.clear()
            if idx < len(entry.chunks) or entry.status != "active":
                continue
            try:
                await asyncio.wait_for(entry.changed.wait(), timeout=keepalive_seconds)
            except TimeoutError:
                yield ": heartbeat\n\n"

    # ------------------------------------------------------------------
    # 清掃
    # ------------------------------------------------------------------

    def _sweep(self) -> None:
        """Lazy 清掃：移除完成超過 TTL 的 entry；終結超齡 active（孤兒保險）。"""
        now = float(get_current_epoch())
        stale: list[str] = []
        for sid, entry in self._streams.items():
            if entry.status != "active":
                if entry.finished_at is not None and (
                    now - entry.finished_at > STREAM_BUFFER_TTL_SECONDS
                ):
                    stale.append(sid)
            elif now - entry.started_at > STREAM_ACTIVE_MAX_SECONDS:
                logger.warning(
                    "🧹 [StreamBuffer] Orphan active stream %s exceeded %ds; cancelling.",
                    sid,
                    STREAM_ACTIVE_MAX_SECONDS,
                )
                self._terminate(entry, "error")
        for sid in stale:
            del self._streams[sid]


def _same_requester(owner: AgentIdentity, identity: AgentIdentity) -> bool:
    return owner.agent_id == identity.agent_id and owner.is_human == identity.is_human


# 模組級單例（SSOT）
stream_buffer_service = StreamBufferService()
