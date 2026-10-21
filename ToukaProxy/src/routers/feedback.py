"""使用者回饋路由 (Feedback Router) — HAX G15/G6，ADR-0084.

讚/倒讚＋選填原因的提交、還原與檢視。回饋存於隔離的 feedback.db，不接觸 agent
soul／記憶系統，僅供人工檢視。倒讚原因欄同時兼作有害/偏見內容回報管道 (G6)。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.authorization_gateway import require_authenticated, require_owner_or_admin
from src.services.feedback_store import FeedbackStore

router = APIRouter(prefix="/feedback", tags=["feedback"])

# 任何有效身分即可提交/讀取自身回饋。
_require_authenticated = require_authenticated


# 檢視全體回饋僅限 Owner/admin。
_require_owner_role = require_owner_or_admin


class FeedbackSubmitRequest(BaseModel):
    """提交一則回饋。"""

    session_id: str = Field(..., description="對話 session ID")
    message_id: str = Field(..., description="訊息樹節點 ID")
    feedback_type: Literal["up", "down", "none"] = Field(
        ..., description="up=讚 down=倒讚 none=撤回"
    )
    reason: str | None = Field(
        None, max_length=500, description="選填原因（倒讚兼有害/偏見內容回報）"
    )


class FeedbackSubmitResponse(BaseModel):
    """提交結果。"""

    status: str = Field("success")
    feedback_id: str | None = Field(None, description="回饋 ID；撤回時為 null")


class FeedbackState(BaseModel):
    """單則回饋狀態（供前端還原按鈕）。"""

    type: str
    reason: str | None = None


class SessionFeedbackResponse(BaseModel):
    """某 session 的回饋對映。"""

    feedbacks: dict[str, FeedbackState]


class FeedbackItem(BaseModel):
    """檢視頁的單筆回饋（v1 不外露提交者身分；單一擁有者情境足夠）。"""

    id: str
    session_id: str
    message_id: str
    feedback_type: str
    reason: str | None = None
    created_at: int
    updated_at: int


@router.post("/submit", response_model=FeedbackSubmitResponse)
async def submit_feedback(
    req: FeedbackSubmitRequest,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> FeedbackSubmitResponse:
    """提交/更新/撤回對某則 AI 回覆的回饋。"""
    feedback_id = await FeedbackStore.submit(
        session_id=req.session_id,
        message_id=req.message_id,
        identity=identity,
        feedback_type=req.feedback_type,
        reason=req.reason,
    )
    return FeedbackSubmitResponse(status="success", feedback_id=feedback_id)


@router.get("/session/{session_id}", response_model=SessionFeedbackResponse)
async def get_session_feedback(
    session_id: str,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> SessionFeedbackResponse:
    """回傳當前身分於此 session 的回饋對映（供前端還原按鈕狀態）。"""
    mapping = await FeedbackStore.get_session_map(identity, session_id)
    return SessionFeedbackResponse(
        feedbacks={
            mid: FeedbackState(type=state["type"], reason=state["reason"])
            for mid, state in mapping.items()
        }
    )


@router.get("/recent", response_model=list[FeedbackItem])
async def get_recent_feedback(
    limit: int = 50,
    _identity: AgentIdentity = Depends(_require_owner_role),  # noqa: B008
) -> list[FeedbackItem]:
    """檢視頁用：最近回饋清單（僅 Owner/admin）。"""
    rows = await FeedbackStore.get_recent(limit)
    return [FeedbackItem(**row) for row in rows]
