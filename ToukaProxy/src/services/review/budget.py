"""ReviewBudgetLedger——互審預算硬界（互審設計文件 §4.6，ADR-0137）.

四道硬上限的三道住這裡（第四道 attempts 累計落在 Task.review_attempts_total）：
- **往返 CAP**：``GLOBAL_REVIEW_CAP = 3``（決策 1）為程式內 SSOT 天花板；
  config **只能收緊不能放寬**（evolution ``_clamp_quota`` 慣例）——放寬＝修
  天花板＝一次人審 commit；
- **深度**：兩層行為承諾——主 agent context（depth 0）可開子 loop，
  子（depth >= 1）不可再開孫（``depth_allows_spawn``）；
- **spawn 記帳**：mission 級 ledger 單例＋**單一 mission 級 lock** 防
  lost-update（設計 §9 殘餘風險 6 的正面解）；**刻意不用** rhythm_service——
  其 ``initialize_session`` 對同 request_id 無條件重設＝子 loop 進入即回血、
  結構性聚合不了（已核碼）；
- **成本閘**：互審路徑 cap 有數字即強制查（**無視 ``cap.enabled``**——互審
  成本閘不可關；與 ``is_over_budget`` 的 fail-open 相反）；無數字＝誠實不擋
  （無法對沒有數字的天花板強制執行）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.models.identity import AgentIdentity  # noqa: TC001 — 執行期型別（測試以真身分呼叫）
from src.services.autonomy import autonomy_service

logger = logging.getLogger(__name__)

# ADR-0137 決策 1：CAP=3、兩層深度。
GLOBAL_REVIEW_CAP = 3
MAX_REVIEW_DEPTH = 1  # 主(0) 可開子；子(>=1) 不可再開＝「兩層」行為承諾
DEFAULT_SPAWN_CEILING = 12  # 每 mission 子 loop（producer+critic 合計）開啟上限


def effective_review_cap(configured: int | None) -> int:
    """Config 只能收緊不能放寬：``min(GLOBAL_REVIEW_CAP, configured)``、下限 1。"""
    if configured is None:
        return GLOBAL_REVIEW_CAP
    return max(1, min(GLOBAL_REVIEW_CAP, int(configured)))


def depth_allows_spawn(review_depth: int) -> bool:
    """兩層承諾：depth 0（主 agent context）可開子 loop；子（>=1）不可再開。"""
    return review_depth < MAX_REVIEW_DEPTH


class ReviewBudgetLedger:
    """mission 級 spawn 記帳（單例；每 mission 一把 asyncio.Lock 防 lost-update）。"""

    def __init__(self) -> None:
        """初始化空帳本與鎖表。"""
        self._counts: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, mission_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(mission_id, asyncio.Lock())

    async def register_spawn(
        self, mission_id: str, *, ceiling: int = DEFAULT_SPAWN_CEILING
    ) -> bool:
        """登記一次子 loop 開啟；達 ceiling 回 False（拒開），否則入帳回 True。"""
        lock = await self._lock_for(mission_id)
        async with lock:
            current = self._counts.get(mission_id, 0)
            if current >= ceiling:
                logger.warning(
                    "review spawn ceiling reached for mission %s (%d)", mission_id, ceiling
                )
                return False
            self._counts[mission_id] = current + 1
            return True

    def spawned(self, mission_id: str) -> int:
        """回傳該 mission 已開啟的子 loop 數。"""
        return self._counts.get(mission_id, 0)


def cost_ceiling_exceeded(
    identity: AgentIdentity, *, config: dict[str, Any] | None
) -> bool:
    """互審成本閘：cap 有數字即強制查（無視 cap.enabled——互審路徑不可關）。"""
    cap = (config or {}).get("cap") or {}
    limit = cap.get("max_total_cost_usd")
    if limit is None:
        return False
    return autonomy_service.get_total_cost_usd(identity) >= float(limit)


# ---------------------------------------------------------------------------
# Singleton（可被測試重置——dispatch_callback_store 同款）
# ---------------------------------------------------------------------------

_ledger: ReviewBudgetLedger | None = None


def get_review_ledger() -> ReviewBudgetLedger:
    """回傳模組級 singleton ReviewBudgetLedger。"""
    global _ledger  # noqa: PLW0603
    if _ledger is None:
        _ledger = ReviewBudgetLedger()
    return _ledger


def _reset_review_ledger() -> None:
    """測試專用：重置 singleton，確保測試間隔離。"""
    global _ledger  # noqa: PLW0603
    _ledger = None
