"""自主行為配置模型 (Autonomy Models)."""

from pydantic import BaseModel, Field, field_validator

from .permissions import BasePolicyModel


class GlobalConfig(BaseModel):
    """全域自主限制配置。"""
    max_pulses_per_session: int = 5

class ReactLoopConfig(BaseModel):
    """ReAct 工具迴圈保險絲（ADR-0146）。

    max_tool_rounds：單次 run 的工具輪上限（一輪＝一次含工具調用的模型回合）。
    0＝不限——僅對「有人在場」的互動 run 生效；無人 run（heartbeat 及其鏈路）
    一律有限：排程明示值＞本保險絲＞程式保底 HEADLESS_FALLBACK_ROUNDS(50)。
    舊制 nudge 續圈（default_max_slots/stop_keywords/nudge_prompt）已移除，
    載入層自動遷移。
    """
    max_tool_rounds: int = Field(default=0, ge=0)

class SelfRepairConfig(BaseModel):
    """自動自癒/自我修復限制。"""
    enabled: bool = True
    max_attempts: int = 3
    repair_prompt: str = "\n[REPAIR] 指令執行失敗 (退出碼: {exit_code})。這是第 {attempts}/{max_attempts} 次嘗試。請修正錯誤後重試。"

class QuotaConfig(BaseModel):
    """每日行動配額（每位代理人每日可自主行動的次數上限）。"""
    daily_max: int = 50

class CapConfig(BaseModel):
    """累計成本上限閘（opt-in）：啟用後，代理人累計成本達上限即停止自主續跑。

    enabled 預設 False（不改變現有行為）；要啟用請設 enabled=True 並給定
    max_total_cost_usd（USD）。max_total_cost_usd 為 None 時即使 enabled 也不擋
    （視為未設上限）。
    """
    enabled: bool = False
    max_total_cost_usd: float | None = None

class SandboxLimits(BaseModel):
    """沙盒運行資源配額限制。"""
    execution_timeout_sec: int = 30
    max_storage_mb: int = 50
    max_memory_mb: int = 256

class AutoReviewConfig(BaseModel):
    """Auto 審查員（ADR-0246 U2c-3）。

    model 預設空白＝未設定：每次要審的動作都以「審查員未設定」轉人（有人在場）或拒絕（無人
    在場）。headless_terminal 預設 False：無人在場的終端指令直接拒絕、不送審（ADR-0246 決定 8）。
    timeout_s 預設 60（r55，ADR-0271）：逾時＝轉人／拒絕，不是放行條件。
    """
    model: str = Field(default="", max_length=200)
    max_tokens: int = Field(default=512, ge=64, le=4096)
    timeout_s: float = Field(default=60.0, gt=0, le=120)
    headless_terminal: bool = False

    @field_validator("model")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        return value.strip()

class ReviewerModelOption(BaseModel):
    """審查員選單的一個選項（ADR-0246）。"""
    id: str
    name: str
    provider: str

class ReviewerModelsResponse(BaseModel):
    """審查員模型選單：只列供應商金鑰已設定的一般模型；不自動帶預設值。"""
    models: list[ReviewerModelOption]

class AutonomyConfigModel(BasePolicyModel):
    """自主行為系統配置模型 (SSOT)"""
    global_config: GlobalConfig = Field(default_factory=GlobalConfig)
    react_loop: ReactLoopConfig = Field(default_factory=ReactLoopConfig)
    self_repair: SelfRepairConfig = Field(default_factory=SelfRepairConfig)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    cap: CapConfig = Field(default_factory=CapConfig)
    sandbox_limits: SandboxLimits = Field(default_factory=SandboxLimits)
    auto_review: AutoReviewConfig = Field(default_factory=AutoReviewConfig)
