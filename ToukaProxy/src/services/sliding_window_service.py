"""滑動視窗對話管理服務 (Sliding Window History Service — SOLID-SRP).

職責：
- 計算對話來回數（user+assistant pair）
- 判斷是否需要剪斷
- 計算剪斷分割點（保留完整 pair，不破壞語義邊界）
- ACID 安全地執行「先歸檔進舊對話庫 → 再更新 Session」的雙寫操作

觸發點：由 heartbeat._process_heartbeat_loop 在 PostHeartbeat 呼叫，
僅對 is_home_mode=True 的家對話生效。

依賴（DIP）：
- sliding_window_config — 設定檔讀取
- soul_prelude_service／soul_prelude_merge — 剪下的寫進前情、剪完合併（ADR-0305）
- LegacySyncService — 剪下的對話直接歸檔進舊對話庫（``legacy/`` 受保護，不寫中繼 JSON 檔）
- session_service   — 讀寫 SQLite ui_sessions.db
- TreeMessageManager — 線性路徑還原與樹狀結構重建
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from memory_server.tools import get_manager

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.session_persistence import SavedResult, SessionChanges
from src.services import destructive_ops, soul_prelude_merge, soul_prelude_service
from src.services.dialogue_summarizer import summarize_dialogue
from src.services.hooks.lifecycle import notify_post_compact, notify_pre_compact
from src.services.models.context_window_registry import get_context_window
from src.services.sliding_window_config import load_sliding_window_config
from src.utils.async_db import SessionPersistenceError, session_service
from src.utils.history_manager import TreeMessageManager, count_rounds_from_linear
from src.utils.soul_prelude_db import insert_segment_tx, list_live_segments
from src.utils.task_utils import spawn_logged
from src.utils.time_utils import get_current_epoch, get_current_epoch_ms
from src.utils.token_estimator import estimate_messages_tokens

if TYPE_CHECKING:
    import aiosqlite

    from src.models.sliding_window import SlidingWindowConfig
    from src.utils.soul_prelude_db import PreludeSegment

logger = logging.getLogger(__name__)

# 剪裁中途要不要放棄：拿到讀到的存檔（存回前是 None），回 True 就不剪
type Abandon = Callable[[dict[str, Any] | None], bool]


async def _write_segment_unless_disturbed(
    db: aiosqlite.Connection, *, seg: PreludeSegment, identity: AgentIdentity, since: int
) -> None:
    """剪裁交易裡寫前情段落；摘要期間開始重置或刪除就丟例外，整筆退回（審查 F11）."""
    if destructive_ops.disturbed(identity, since):
        raise ToukaException(ErrorCode.AGENT_RESET_IN_PROGRESS)
    await insert_segment_tx(db, seg)


class SlidingWindowService:
    """滑動視窗對話管理服務（單一職責）。"""

    def __init__(self) -> None:
        """初始化 per-session 換模型冷卻記錄與並發鎖。

        in-memory 狀態為軟性記錄，重啟即重置可接受（ADR-0092 Phase C/D）。
        """
        self._last_seen_model: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        """取得 per-session 鎖，序列化 heartbeat/chat 兩觸發點（ADR-0092 Phase D / 禁令 57）。"""
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    # ── Config ─────────────────────────────────────────────────────────────

    def load_config(self) -> SlidingWindowConfig:
        """從 sliding_window.json 載入設定，失效時回退預設值（讀法在 sliding_window_config）。"""
        return load_sliding_window_config()

    # ── Pure functions ──────────────────────────────────────────────────────

    def count_rounds(self, messages: list[dict[str, Any]]) -> int:
        """委派至 history_manager SSOT（count_rounds_from_linear）。"""
        return count_rounds_from_linear(messages)

    def should_trim(self, usage_pct: float, config: SlidingWindowConfig) -> bool:
        """判斷 context 占用是否達剪裁觸發點（ADR-0092 Phase C）。"""
        if not config.enabled:
            return False
        return usage_pct >= config.trigger_pct

    def compute_split(
        self,
        messages: list[dict[str, Any]],
        keep_rounds: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """計算剪斷分割點，回傳 (legacy_messages, kept_messages)。

        分割點保證在完整 pair 邊界：不切斷 user+assistant 對。
        kept_messages[0].parentId 重設為 None（重定錨）。

        若來回數不超過 keep_rounds，legacy 回傳空列表。
        """
        pairs: list[tuple[int, int]] = []
        i = 0
        while i < len(messages):
            if messages[i].get("role") == "user":
                j = i + 1
                while j < len(messages) and messages[j].get("role") not in (
                    "user",
                    "assistant",
                ):
                    j += 1
                if j < len(messages) and messages[j].get("role") == "assistant":
                    pairs.append((i, j))
                    i = j + 1
                else:
                    i += 1
            else:
                i += 1

        if len(pairs) <= keep_rounds:
            return ([], list(messages))

        trim_pairs = len(pairs) - keep_rounds
        split_idx = pairs[trim_pairs][0]  # 第一個保留 pair 的 user 訊息索引

        legacy = list(messages[:split_idx])
        kept = [dict(m) for m in messages[split_idx:]]

        # 重定錨：切斷與 legacy 的父子關係
        if kept:
            kept[0]["parentId"] = None

        return (legacy, kept)

    # ── Token-based split (ADR-0092 Phase C) ─────────────────────────────────

    @staticmethod
    def _pair_indices(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
        """回傳完整 user+assistant pair 的 (user_idx, assistant_idx) 清單。"""
        pairs: list[tuple[int, int]] = []
        i = 0
        while i < len(messages):
            if messages[i].get("role") == "user":
                j = i + 1
                while j < len(messages) and messages[j].get("role") not in (
                    "user",
                    "assistant",
                ):
                    j += 1
                if j < len(messages) and messages[j].get("role") == "assistant":
                    pairs.append((i, j))
                    i = j + 1
                else:
                    i += 1
            else:
                i += 1
        return pairs

    def compute_split_by_tokens(
        self,
        messages: list[dict[str, Any]],
        model: str,
        window: int,
        config: SlidingWindowConfig,
        current_total: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """剪到 token 地板：保留最新對話直到 context 占用 ≤ target_floor_pct。

        回傳 (legacy_messages, kept_messages)。保證完整 pair 邊界、保底
        min_keep_pairs、kept[0] 重定錨。對話太小或無需剪 → legacy 回空。
        """
        pairs = self._pair_indices(messages)
        if len(pairs) <= config.min_keep_pairs:
            return ([], list(messages))

        # 對話可保留的 token 預算 = 地板 token − overhead（不可剪的 system/記憶/工具）。
        dialogue_total = estimate_messages_tokens(messages, model)
        overhead = max(0, int(current_total or 0) - dialogue_total)
        target_tokens = window * config.target_floor_pct / 100.0
        budget = target_tokens - overhead

        # 後綴 token 和（messages[idx:] 的估算 token），供 O(n) 求保留量。
        suffix_at: list[int] = [0] * (len(messages) + 1)
        running = 0
        for idx in range(len(messages) - 1, -1, -1):
            running += estimate_messages_tokens([messages[idx]], model)
            suffix_at[idx] = running

        # 從「保留最多」往下找第一個保留 token ≤ budget 者；全超出（overhead 太大）
        # → 保底 min_keep_pairs。
        keep_count = config.min_keep_pairs
        for k in range(len(pairs), config.min_keep_pairs - 1, -1):
            split_idx = pairs[len(pairs) - k][0]
            if suffix_at[split_idx] <= budget:
                keep_count = k
                break

        split_idx = pairs[len(pairs) - keep_count][0]
        legacy = list(messages[:split_idx])
        if not legacy:
            return ([], list(messages))
        kept = [dict(m) for m in messages[split_idx:]]
        if kept:
            kept[0]["parentId"] = None
        return (legacy, kept)

    # ── Token usage helpers (ADR-0092 Phase C) ───────────────────────────────

    @staticmethod
    def _resolve_model(linear: list[dict[str, Any]]) -> str:
        """取對話最近一則訊息的 model（決定 context window 分母）。"""
        for msg in reversed(linear):
            if isinstance(msg, dict):
                m = msg.get("model")
                if isinstance(m, str) and m:
                    return m
        return ""

    @staticmethod
    def _current_total_tokens(linear: list[dict[str, Any]], model: str) -> int:
        """估當前完整 context token 數。

        優先：最近一則帶 authoritative token_usage 的訊息（= 上一輪完整 context
        大小、含 overhead）。無則 fallback 本地估對話 token（偏低、無 overhead）。
        """
        for msg in reversed(linear):
            if not isinstance(msg, dict):
                continue
            prog = msg.get("progress")
            if isinstance(prog, dict):
                tu = prog.get("token_usage")
                if isinstance(tu, dict):
                    tot = tu.get("total_tokens")
                    if isinstance(tot, (int, float)) and tot > 0:
                        return int(tot)
        return estimate_messages_tokens(linear, model)

    def _check_model_cooldown(self, session_id: str, model: str) -> bool:
        """換模型冷卻：回 True 表示『剛換模型，本輪應跳過剪裁』（並更新 last-seen）。"""
        prev = self._last_seen_model.get(session_id)
        self._last_seen_model[session_id] = model
        return prev is not None and bool(model) and prev != model

    # ── Tree reconstruction ─────────────────────────────────────────────────

    @staticmethod
    def _rebuild_tree(messages: list[dict[str, Any]]) -> dict[str, Any]:
        """將線性訊息列表重建為 TreeHistory 字典格式。

        重建後的樹只含線性主幹，適合存入 ui_sessions.db。
        """
        if not messages:
            return TreeMessageManager.create_empty_tree()

        tree_msgs: dict[str, dict[str, Any]] = {}
        for i, msg in enumerate(messages):
            node = dict(msg)
            # 修正 parentId 鏈（以免舊 id 指向已刪除節點）
            node["parentId"] = messages[i - 1]["id"] if i > 0 else None
            # childrenIds：供前端導覽用
            if i < len(messages) - 1:
                node["childrenIds"] = [messages[i + 1]["id"]]
            else:
                node["childrenIds"] = []
            tree_msgs[node["id"]] = node

        return {
            "messages": tree_msgs,
            "currentId": messages[-1]["id"],
        }

    # ── Legacy export format ────────────────────────────────────────────────

    @staticmethod
    def _format_legacy_export(
        session_id: str,
        title: str,
        messages: list[dict[str, Any]],
        identity: AgentIdentity,
    ) -> list[dict[str, Any]]:
        """將線性訊息格式化為 LegacySyncService 可識別的 JSON 結構。"""
        agent_id = identity.agent_id
        return [
            {
                "id": session_id,
                "title": title,
                "agent_id": agent_id,
                "exported_at": int(get_current_epoch()),
                "messages": [
                    {
                        "id": m.get("id", ""),
                        "role": m.get("role", ""),
                        "content": m.get("content", ""),
                        "timestamp": m.get("timestamp"),
                    }
                    for m in messages
                ],
            }
        ]

    # ── Main entry point ────────────────────────────────────────────────────

    async def check_and_trim(
        self,
        identity: AgentIdentity,
        session_id: str,
        abandon: Abandon | None = None,
    ) -> bool:
        """ACID-safe 滑動視窗主入口。

        ADR-0092 Phase D / 禁令 57：以 per-session 鎖序列化 heartbeat 與 chat
        兩授權觸發點，避免兩條路徑同時讀改同一 session 樹造成競態。
        ``abandon``（聊天側，ADR-0302）：讀到存檔後、寫摘要前（重讀一次）、存回前各問一次，
        回 True 就不剪；存回前傳 None。
        剪成功後在對話鎖外、在背景做前情合併（拿靈魂鎖，ADR-0305 鎖的順序；審查 F4），剪裁不等它；
        合併失敗只記 log。
        """
        async with self._get_lock(session_id):
            trimmed = await self._check_and_trim_inner(identity, session_id, abandon)
        if trimmed:
            merge = soul_prelude_merge.merge_if_needed(
                identity, self.load_config(), home_id=session_id
            )
            spawn_logged(merge, name=f"prelude-merge:{session_id}")
        return trimmed

    @staticmethod
    async def _previous_entry(identity: AgentIdentity, session_id: str) -> str | None:
        """前情顯示順序的最後一段（給摘要員當背景，避免重複；審查 F3）；讀不到就不給."""
        try:
            segments = await list_live_segments(soul_prelude_service.prelude_agent(identity))
        except Exception:
            logger.warning("SlidingWindow: prelude unavailable for the summarizer", exc_info=True)
            return None
        visible = soul_prelude_service.visible_segments(segments, session_id)
        return visible[-1].content if visible else None

    async def _moved_on(
        self, identity: AgentIdentity, session_id: str, revision: str, abandon: Abandon
    ) -> bool:
        """寫摘要前重讀一次：存檔已經變了或該放棄，就省下這次模型呼叫。"""
        try:
            fresh = await session_service.read_persistence_snapshot(session_id, identity)
        except SessionPersistenceError:
            return True
        return fresh.revision != revision or abandon(fresh.session)

    async def _check_and_trim_inner(
        self,
        identity: AgentIdentity,
        session_id: str,
        abandon: Abandon | None = None,
    ) -> bool:
        """check_and_trim 的實際邏輯（呼叫端已持 per-session 鎖）。

        ACID 安全順序：
        1. (ACID-A) 歸檔進舊對話庫（一個交易）→ 確認成功
        2. (ACID-B) 更新 Session（SQLite）→ 若失敗，legacy 已存，資料不損

        回傳 True 表示剪斷成功完成，False 表示無需剪斷或剪斷被安全跳過。
        """
        config = self.load_config()
        if not config.enabled:
            return False
        # 靈魂正在重置或刪除時不剪（前情段落與舊對話庫都寫不得，審查 F11、第 2 輪 G6）；
        # 代數在讀存檔之前取，之後開始的重置在歸檔前與寫段落時都看得到
        since = destructive_ops.generation(identity)
        if since is None:
            logger.info("SlidingWindow: %s is being reset or deleted; not trimming", session_id)
            return False

        # ── 1. 讀取 session 與線性路徑 ──────────────────────────────────────
        try:
            snapshot = await session_service.read_persistence_snapshot(session_id, identity)
        except SessionPersistenceError:
            logger.warning("SlidingWindow: persistence snapshot unavailable; skipping trim")
            return False
        session = snapshot.session
        if not session:
            logger.debug("SlidingWindow: session %s not found, skipping", session_id)
            return False
        session_service.require_scope(session, identity)
        # 只剪家對話；呼叫端判斷之後才換家、取消家就不剪（存回時交易內再查一次，ADR-0301）
        if not session.get("is_home") or (abandon is not None and abandon(session)):
            return False

        raw_messages = session.get("messages", {})
        linear = TreeMessageManager.get_linear_path(raw_messages)
        if not linear:
            return False

        # ── 2. 計算 context 占用百分比（token）─────────────────────────────
        model = self._resolve_model(linear)
        window = get_context_window(model)

        # 換模型冷卻：剛換模型本輪不剪，下輪 model 相符才剪。
        if self._check_model_cooldown(session_id, model):
            logger.info(
                "SlidingWindow: model switched to '%s' for session %s; cooldown this round.",
                model,
                session_id,
            )
            return False

        current_total = self._current_total_tokens(linear, model)
        usage_pct = min(100.0, current_total / window * 100) if window > 0 else 0.0
        if not self.should_trim(usage_pct, config):
            logger.debug(
                "SlidingWindow: usage %.1f%% < trigger %d%%, no trim needed",
                usage_pct,
                config.trigger_pct,
            )
            return False

        logger.info(
            "SlidingWindow: session %s at %.1f%% (trigger=%d%%, window=%d). "
            "Trimming toward floor %d%%.",
            session_id,
            usage_pct,
            config.trigger_pct,
            window,
            config.target_floor_pct,
        )

        # ── 3. 計算分割點（剪到 token 地板）────────────────────────────────
        legacy_msgs, kept_msgs = self.compute_split_by_tokens(
            linear, model, window, config, current_total
        )
        if not legacy_msgs:
            return False

        agent_id = identity.agent_id
        # 開始之後才進入重置或刪除的：歸檔進舊對話庫之前就停（審查第 2 輪 G6）
        if destructive_ops.disturbed(identity, since):
            logger.info("SlidingWindow: %s is being reset or deleted; not trimming", agent_id)
            return False
        notify_pre_compact(identity, session_id)

        # ── 4. ACID-A: 歸檔進舊對話庫（先於 session 更新；失敗就不剪）──────
        label = f"{agent_id}_sw_{int(get_current_epoch())}.json"
        export_data = self._format_legacy_export(
            session_id,
            session.get("title", "sliding_window_export"),
            legacy_msgs,
            identity,
        )
        try:
            archived = await asyncio.to_thread(
                get_manager().get_legacy_sync_service().archive_payload,
                identity,
                export_data,
                label,
            )
            logger.info("SlidingWindow: archived %d msgs as %s", archived, label)
        except Exception:
            logger.exception(
                "SlidingWindow: ACID-A failed — legacy archive error for %s. Session unchanged.",
                session_id,
            )
            return False
        if not archived:  # 沒歸檔到任何訊息就不剪，剪了會直接遺失
            logger.error("SlidingWindow: nothing archived for %s. Session unchanged.", session_id)
            return False

        # ── ACID-S: 摘要（best-effort、不進關鍵路徑；失敗→None→降級成一行來源註記）──
        # 寫進前情（ADR-0305），不再改 context_summary；摘要員拿前情最後一段當背景。
        # 摘要期間開始重置或刪除的，寫段落時在交易裡退回（審查 F11）。
        summary: str | None = None
        if abandon is not None and await self._moved_on(
            identity, session_id, snapshot.revision, abandon
        ):
            return False
        if config.summary_budget_pct > 0:
            # 摘要最多前情上限的一半（和合併保留的額度一致），一段擠不掉整份前情（審查 F6、G10）
            budget_tokens = min(
                int(window * config.summary_budget_pct / 100), config.prelude_max_tokens // 2
            )
            summary = await summarize_dialogue(
                identity,
                legacy_msgs,
                budget_tokens,
                config.summary_model,
                previous=await self._previous_entry(identity, session_id),
            )

        # ── 5. ACID-B: 更新 Session（只在 legacy 確認寫入後）；前情段落同一個交易 ──
        if abandon is not None and abandon(None):
            return False
        new_tree = self._rebuild_tree(kept_msgs)
        segment = soul_prelude_service.trim_segment(
            identity, session_id, legacy_msgs, summary, now_ms=get_current_epoch_ms()
        )
        try:
            changes = SessionChanges(
                messages=new_tree, updatedAt=int(get_current_epoch() * 1000)
            )
            success = await session_service.conditional_save_session(
                session_id,
                snapshot.revision,
                changes,
                identity,
                require_home=True,
                after_write=functools.partial(
                    _write_segment_unless_disturbed, seg=segment, identity=identity, since=since
                ),
            )
        except Exception:
            logger.exception(
                "SlidingWindow: ACID-B failed — session update error for %s. "
                "Legacy already archived as %s; session outcome is not confirmed.",
                session_id,
                label,
            )
            return False

        if not isinstance(success, SavedResult):
            if destructive_ops.disturbed(identity, since):  # 重置或刪除開始了：整筆退回是預期的
                logger.info(
                    "SlidingWindow: trim of %s rolled back; reset or delete started", session_id
                )
                return False
            logger.error(
                "SlidingWindow: ACID-B not confirmed (including conflict) for session %s. "
                "Legacy archive %s preserved.",
                session_id,
                label,
            )
            return False

        logger.info(
            "SlidingWindow: trimmed session %s. Legacy: %d msgs → %s. Kept: %d msgs.",
            session_id,
            len(legacy_msgs),
            label,
            len(kept_msgs),
        )
        notify_post_compact(identity, session_id, summary)
        return True


sliding_window_service = SlidingWindowService()
