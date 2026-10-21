"""滑動視窗設定模型 (Sliding Window Config — SSOT).

實作 PolicyAsCode 原則：設定以強型別 Pydantic 模型定義，
唯一物理來源為 ToukaProxy/config/sliding_window.json。
"""
from __future__ import annotations

from pydantic import Field, field_validator, model_validator
from src.models.permissions import BasePolicyModel


class SlidingWindowValidationError(ValueError):
    """滑動視窗驗證異常。"""


class SlidingWindowKeepRoundError(SlidingWindowValidationError):
    """keep_rounds 驗證錯誤。"""
    def __init__(self) -> None:
        """初始化保留回合數上限異常."""
        super().__init__("keep_rounds must be >= 1")


class SlidingWindowLimitRoundError(SlidingWindowValidationError):
    """upper_limit_rounds 驗證錯誤。"""
    def __init__(self) -> None:
        """初始化觸發回合數上限異常."""
        super().__init__("upper_limit_rounds must be greater than keep_rounds")


class SlidingWindowPctError(SlidingWindowValidationError):
    """token 百分比不變式錯誤（ADR-0092 Phase C）。"""
    def __init__(self) -> None:
        """初始化百分比不變式異常."""
        super().__init__(
            "target_floor_pct + summary_budget_pct must be < trigger_pct"
        )


class SlidingWindowConfig(BasePolicyModel):
    """滑動視窗對話紀錄設定。

    僅適用於家對話（home session）；子對話一律排除。
    觸發時機：PostHeartbeat（所有 pulse 跑完後）。

    ADR-0092 Phase C：剪裁基準由「對話則數」改為「context window token 百分比」。
    `keep_rounds`/`upper_limit_rounds` 保留為相容欄位（Phase E 移除）。
    """

    enabled: bool = Field(default=False, description="是否啟用滑動視窗功能")
    # ── 則數規則（deprecated，ADR-0092 Phase E 移除）──
    keep_rounds: int = Field(
        default=100, ge=1, description="[deprecated] 剪斷後保留的最少來回數"
    )
    upper_limit_rounds: int = Field(
        default=150, ge=2, description="[deprecated] 超過此來回數才觸發剪斷"
    )
    # ── token 百分比規則（ADR-0092 Phase C，現行）──
    trigger_pct: int = Field(
        default=90, ge=1, le=100, description="context 占用達此百分比才觸發剪裁"
    )
    target_floor_pct: int = Field(
        default=50, ge=0, le=100, description="無損剪掉最舊對話直到 context 占用 ≤ 此地板"
    )
    summary_budget_pct: int = Field(
        default=0, ge=0, le=100, description="摘要 token 上限佔窗百分比（Phase C 預設 0=不摘要）"
    )
    min_keep_pairs: int = Field(
        default=2, ge=1, description="overhead 過大無法降到地板時的最低保留 pair 數"
    )
    summary_model: str = Field(
        default="claude-haiku-4-5",
        description="壓縮被剪對話為摘要所用的便宜模型（ADR-0092 Phase D）",
    )

    @field_validator("keep_rounds")
    @classmethod
    def keep_must_be_positive(cls, v: int) -> int:
        """Validate keep_rounds >= 1."""
        if v < 1:
            raise SlidingWindowKeepRoundError()
        return v

    @model_validator(mode="after")
    def upper_must_exceed_keep(self) -> SlidingWindowConfig:
        """Validate upper_limit_rounds > keep_rounds (deprecated 欄位相容性)。"""
        if self.upper_limit_rounds <= self.keep_rounds:
            raise SlidingWindowLimitRoundError()
        return self

    @model_validator(mode="after")
    def pct_invariant(self) -> SlidingWindowConfig:
        """剪後（地板＋摘要）必須 < 觸發點，否則剪完立即重觸發迴圈。"""
        if self.target_floor_pct + self.summary_budget_pct >= self.trigger_pct:
            raise SlidingWindowPctError()
        return self
