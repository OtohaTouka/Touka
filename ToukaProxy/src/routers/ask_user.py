"""ask_user 答案端點（HAX G10，ADR-0085）.

前端 AskUserModal 作答後 POST 至此，交付答案給 ask_user_store，解除 ask_user 工具的等待。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.ask_user_store import get_ask_user_store
from src.services.authorization_gateway import require_authenticated

router = APIRouter(prefix="/ask_user", tags=["ask_user"])


_require_authenticated = require_authenticated


class AskUserAnswerRequest(BaseModel):
    """依問題順序的答案陣列；每題為其選定/填寫的字串清單。"""

    answers: list[list[str]] = Field(default_factory=list)


class AskUserAnswerResponse(BaseModel):
    """交付結果。"""

    status: str = Field(description="delivered=已交付；unknown_token=token 不存在/已逾時")


@router.post("/answer/{token}", response_model=AskUserAnswerResponse)
async def submit_answer(
    token: str,
    req: AskUserAnswerRequest,
    _identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> AskUserAnswerResponse:
    """交付使用者對 ask_user 提問的答案。"""
    delivered = get_ask_user_store().deliver_answer(token, req.answers)
    return AskUserAnswerResponse(status="delivered" if delivered else "unknown_token")
