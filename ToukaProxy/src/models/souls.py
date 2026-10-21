"""靈魂核心設定資料結構描述 (Soul Core Configurations Schema).

定義 L1 靈魂核心的運行配置 (SoulConfig) 與建立／更新／刪除請求的 Pydantic 模型。
"""

from pydantic import BaseModel, Field


class SoulConfig(BaseModel):
    """靈魂核心的詳細運行配置"""
    base_model: str | None = Field(None, description="大腦型號 (如 claude-3-5-sonnet)")
    temperature: float = 0.7
    top_p: float = 1.0
    top_k: int = 40
    enabled_tools: list[str] = Field(default_factory=list)
    safety_settings: list[dict[str, str]] = Field(default_factory=list)
    is_toukasoul: bool = True

class SoulCreateRequest(BaseModel):
    """建立靈魂核心的請求模型"""
    agent_id: str = Field(..., description="唯一識別碼 (ID)")
    name: str | None = None
    persona: str | None = None
    config: SoulConfig

class SoulUpdateRequest(BaseModel):
    """更新靈魂核心的請求模型"""
    config: SoulConfig | None = None
    persona: str | None = None
    # D2/D3（ADR-0142）：人類 admin 編輯靈魂資料需步升重驗密碼；agent 自我存取不帶此欄。
    admin_password: str | None = None


class SoulDeleteRequest(BaseModel):
    """刪除靈魂的請求模型：admin 步升密碼（D2）。"""
    admin_password: str
