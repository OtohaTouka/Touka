"""策略上下文與決策結構模型 (Policy Context and Decision Models).

定義 PaC 決策上下文 (PolicyContext) 與決策結果 (PolicyDecision) 的資料結構。
"""

from typing import Any

from pydantic import BaseModel, Field

from .identity import AgentIdentity


class PolicyContext(BaseModel):
    """權限決策上下文 (Context)
    描述一次權限請求的所有相關資訊。
    """
    actor: AgentIdentity = Field(..., description="發起操作的代理人身分")
    resource: str = Field(..., description="被操作的資源 (如指令內容、檔案路徑)")
    action: str = Field(..., description="操作類型 (如 terminal_execute, file_write)")
    environment: dict[str, Any] = Field(default_factory=dict, description="環境資訊 (如目前時間、會話 ID)")

class PolicyDecision(BaseModel):
    """權限決策結果 (Decision)
    封裝權限判定後的結果與執行建議。
    """
    effect: str = Field(..., description="決策結果: ALLOW, DENY, CONFIRM")
    reason: str | None = Field(None, description="決策原因或攔截模式")
    enforced_auth_level: str | None = Field(None, description="強制執行的安全等級 (如 HITL 要求)")
    metadata: dict[str, Any] = Field(default_factory=dict, description="額外的決策元數據 (如配額狀態)")
