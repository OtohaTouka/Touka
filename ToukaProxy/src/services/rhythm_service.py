"""行動節律服務 (Rhythm Service).

負責管理 Agent 的工具輪預算 (Tool-Round Budget) 與中斷旗標。
本服務從 AutonomyService 中抽離，專注於處理 ReAct 循環的生命週期與節奏控制。

ADR-0146 語意翻轉：舊制 slots＝「模型回純文字時系統 nudge 硬續的次數」（③，
已整段移除——純文字＝回合結束，與主流 harness 一致）；新制 rounds＝「② 工具
迴圈（工具調用→結果→再調用）的保險絲」——修前該迴圈**沒有任何上限**。
分頻鐵則：互動（有人在場）預設不限（0＝不限；煞車＝人類中斷＋HITL＋成本閘）；
無人 run（heartbeat 及其子代理）一律有限，全域設 0 也強制退保底。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 鐵則：無人 run 絕不允許無限工具輪（沒人按中斷鈕）。全域保險絲設 0（不限）時，
# headless 解析強制退到此保底值。刻意用程式常數而非設定檔——設定不可弱化鐵則。
HEADLESS_FALLBACK_ROUNDS = 50

# 額度耗盡的收尾提示（三個 handler 共用 SSOT）：以 user 訊息夾帶，配合
# tool_choice=none 的最後一次呼叫，讓模型消化既有工具結果後優雅著陸。
WRAP_UP_NOTE = (
    "（系統）本次行動的工具輪額度已用盡，最後一輪的工具調用未執行。"
    "請根據目前已取得的結果直接總結收尾，不要再嘗試調用工具。"
)


def build_denied_tool_result(reason: str) -> str:
    """額度耗盡輪的工具佔位結果（協定要求每個 tool_call 都要有結果）。"""
    return f"（未執行：{reason}）"


class RhythmService:
    """系統節律中心，管理工具輪預算與中斷旗標.

    Attributes:
        _session_rounds (dict[str, int]): 請求 ID 與剩餘工具輪的對映（僅有限 run）。
        _session_totals (dict[str, int]): 請求 ID 與初始總額的對映（供進度顯示）。
        _interrupted_sessions (set[str]): 已標記為中斷的會話集合。
    """

    def __init__(self) -> None:
        """初始化節律服務."""
        self._session_rounds: dict[str, int] = {}
        self._session_totals: dict[str, int] = {}
        self._interrupted_sessions: set[str] = set()

    def get_effective_rounds(
        self,
        config: dict[str, Any],
        requested_rounds: int | None = None,
        *,
        headless: bool = False,
    ) -> int:
        """解析本次 run 的工具輪上限（0＝不限，僅互動允許）.

        判定順序 (SSOT)：
        1. 明示值（心跳排程的 pulse 深度）——正整數才採納。
        2. 全域保險絲 ``react_loop.max_tool_rounds``（0＝不限）。
        3. headless 鐵則：解析結果 ≤0（不限）時強制退 HEADLESS_FALLBACK_ROUNDS。

        Args:
            config: 自主權設定內容。
            requested_rounds: 排程/呼叫端明示的輪數。
            headless: 是否為無人 run（heartbeat 及其鏈路）。

        Returns:
            int: 工具輪上限；0 表示不限（只可能出現在互動模式）。
        """
        rounds = 0
        if isinstance(requested_rounds, int) and requested_rounds > 0:
            rounds = requested_rounds
        else:
            cfg_val = config.get("react_loop", {}).get("max_tool_rounds", 0)
            if isinstance(cfg_val, int) and cfg_val > 0:
                rounds = cfg_val

        if headless and rounds <= 0:
            return HEADLESS_FALLBACK_ROUNDS
        return max(0, rounds)

    def initialize_session(self, request_id: str, max_rounds: int) -> None:
        """為新 run 初始化工具輪預算（0＝不限）.

        Args:
            request_id: 請求唯一識別碼。
            max_rounds: 工具輪上限；0 表示不限。
        """
        self._session_totals[request_id] = max(0, max_rounds)
        self._session_rounds[request_id] = max(0, max_rounds)
        logger.debug(
            "⏳ [Rhythm] Session %s initialized with %s tool rounds.",
            request_id,
            max_rounds if max_rounds > 0 else "unlimited",
        )

    def consume_round(self, request_id: str) -> bool:
        """進入一輪「含工具調用的模型回合」前扣 1；回傳是否仍有額度.

        不限（total=0）或未初始化（防禦性——絕不把正常請求誤判為耗盡）一律 True。
        耗盡後持續回 False（黏著），由 handler 走「最後一次無工具收尾呼叫」。
        """
        total = self._session_totals.get(request_id, 0)
        if total <= 0:
            return True
        remaining = self._session_rounds.get(request_id, 0)
        if remaining <= 0:
            return False
        self._session_rounds[request_id] = remaining - 1
        return True

    def get_total_rounds(self, request_id: str) -> int:
        """本次 run 的工具輪總額（0＝不限；供進度 current/total 顯示）。"""
        return self._session_totals.get(request_id, 0)

    def get_used_rounds(self, request_id: str) -> int:
        """已消耗的工具輪數（僅有限 run 有意義）。"""
        total = self._session_totals.get(request_id, 0)
        if total <= 0:
            return 0
        return total - self._session_rounds.get(request_id, 0)

    def interrupt_session(self, session_id: str) -> None:
        """標記會話為已中斷.

        Args:
            session_id (str): 會話識別碼。
        """
        self._interrupted_sessions.add(session_id)
        logger.info(f"🛑 [Rhythm] Session {session_id} marked for interruption.")

    def is_interrupted(self, session_id: str) -> bool:
        """檢查會話是否已被中斷.

        Args:
            session_id (str): 會話識別碼。

        Returns:
            bool: 若已中斷回傳 True。
        """
        return session_id in self._interrupted_sessions

    def clear_session(self, session_id: str) -> None:
        """清除會話的所有節律狀態 (預算與中斷標記).

        Args:
            session_id (str): 會話或請求識別碼。
        """
        self._session_rounds.pop(session_id, None)
        self._session_totals.pop(session_id, None)
        self._interrupted_sessions.discard(session_id)

    def clear_interrupt(self, session_id: str) -> None:
        """清除單一會話的中斷標記（不動預算）。

        用於每個新 run（foreground 訊息／背景脈衝 run）的起點重置，
        避免上一次的中斷旗標殘留而誤停下一回合。
        """
        self._interrupted_sessions.discard(session_id)

    def reload_all(self) -> None:
        """重置節律服務的所有內部狀態 (SSOT: 供測試環境使用)。"""
        self._session_rounds.clear()
        self._session_totals.clear()
        self._interrupted_sessions.clear()
        logger.debug("🔄 [Rhythm] All rhythm states reloaded/cleared.")


# 全系統單例
rhythm_service = RhythmService()
