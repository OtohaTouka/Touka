"""終端指令執行與審核結構描述 (Terminal Command Approval Schema).

定義終端危險指令人工介入審核 (HITL) 請求的 Pydantic 資料結構。
"""

from typing import Any

from pydantic import BaseModel, Field


class TerminalApprovalRequest(BaseModel):
    """終端危險指令審核的請求模型"""
    payload: dict[str, Any] | None = Field(None, description="附帶的數據 (如環境變數)")


class InteractionResolutionRequest(BaseModel):
    """行程內 interaction 的結構化回覆；不包含上游 continuation state。"""

    payload: dict[str, Any] | None = Field(
        None,
        description="使用者針對 interaction schema 提交的欄位",
    )
