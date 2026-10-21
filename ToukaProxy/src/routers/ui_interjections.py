"""插話端點（ADR-0249 第 4a 段）：AI 工作中，對話擁有者把補充排進目前的串流。

- 只收人類呼叫者；admin 代打可以，其他代理身分（含非 admin 的 X-Agent-ID 代打）拒絕。
- 用 ``_authorized_stream_state`` 驗串流擁有者與 session 存取，並要求同一串流代
  （``stream_id``）；加入只限進行中的串流。
- 文字只進記憶體佇列，不存檔（無痕也一樣）；取出交給 react_core（U4a-2）。
- 驗證錯誤一律回 ``VALIDATION_ERROR``（沿用 ``SessionPersistenceRoute``）。
"""

from fastapi import APIRouter, Depends

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.interjection import (
    Interjection,
    InterjectionCreateRequest,
    InterjectionListResponse,
)
from src.routers.ui_sessions import SessionPersistenceRoute, _authorized_stream_state
from src.services.authorization_gateway import require_human_requester
from src.services.interjection_queue import interjection_queue

router = APIRouter(
    prefix="/ui/sessions", tags=["UI Interjections"], route_class=SessionPersistenceRoute
)

_require_human_requester = require_human_requester


async def _ensure_current_stream(
    session_id: str, stream_id: str, identity: AgentIdentity, *, active: bool
) -> None:
    """擁有者＋同一串流代；``active`` 時串流還要在進行中。"""
    state = await _authorized_stream_state(session_id, identity)
    if (
        state is None
        or state.get("started") is None
        or state["stream_id"] != stream_id
        or (active and state["status"] != "active")
    ):
        raise ToukaException(ErrorCode.STREAM_NOT_FOUND)


@router.post("/{session_id}/interjections", status_code=201)
async def add_interjection(
    session_id: str,
    stream_id: str,
    req: InterjectionCreateRequest,
    identity: AgentIdentity = Depends(_require_human_requester),  # noqa: B008
) -> Interjection:
    """把一則補充排進目前串流。"""
    await _ensure_current_stream(session_id, stream_id, identity, active=True)
    # 驗證之後到加入之間沒有 await：串流不會在這中間換代或結束。
    return interjection_queue.enqueue(session_id, stream_id, req.text, urgent=req.urgent)


@router.get("/{session_id}/interjections")
async def list_interjections(
    session_id: str,
    stream_id: str,
    identity: AgentIdentity = Depends(_require_human_requester),  # noqa: B008
) -> InterjectionListResponse:
    """列出目前串流的插話。"""
    await _ensure_current_stream(session_id, stream_id, identity, active=False)
    return InterjectionListResponse(
        interjections=interjection_queue.list_items(session_id, stream_id)
    )


@router.delete("/{session_id}/interjections/{interjection_id}")
async def retract_interjection(
    session_id: str,
    interjection_id: str,
    stream_id: str,
    identity: AgentIdentity = Depends(_require_human_requester),  # noqa: B008
) -> Interjection:
    """收回一則還沒交給模型的插話。"""
    await _ensure_current_stream(session_id, stream_id, identity, active=False)
    return interjection_queue.retract(session_id, stream_id, interjection_id)
