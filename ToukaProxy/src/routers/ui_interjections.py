"""插話端點（ADR-0249 第 4a 段）：AI 工作中，對話擁有者把補充排進目前的串流。

- 只收人類呼叫者；admin 代打可以，其他代理身分（含非 admin 的 X-Agent-ID 代打）拒絕。
- 用 ``_authorized_stream_state`` 驗串流擁有者與 session 存取，並要求同一串流代
  （``stream_id``）；加入只限進行中的串流。
- 文字只進記憶體佇列，不存檔（無痕也一樣）；取出交給 react_core（U4a-2）。
- 加入前經 UserPromptSubmit hook（ADR-0292）：擋下就不加入；等過 hook 後重新驗證串流。
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
from src.services.hooks.interjection_hooks import check_interjection
from src.services.interjection_queue import interjection_queue
from src.services.stream_buffer_service import stream_buffer_service
from src.utils.async_db import SessionPersistenceError, session_service

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


async def _refuse_old_home(session_id: str) -> None:
    """舊家不接新的插話；列出、收回照常（換家前就在跑的回覆收得回來，ADR-0306）."""
    try:
        old = await session_service.is_old_home(session_id)
    except SessionPersistenceError:
        raise ToukaException(ErrorCode.SESSION_SAVE_FAILED) from None
    if old:
        raise ToukaException(ErrorCode.SESSION_OLD_HOME_READ_ONLY)


@router.post("/{session_id}/interjections", status_code=201)
async def add_interjection(
    session_id: str,
    stream_id: str,
    req: InterjectionCreateRequest,
    identity: AgentIdentity = Depends(_require_human_requester),  # noqa: B008
) -> Interjection:
    """把一則補充排進目前串流。"""
    await _ensure_current_stream(session_id, stream_id, identity, active=True)
    await _refuse_old_home(session_id)
    incognito = stream_buffer_service.persistence_mode(session_id, stream_id) == "incognito"
    fields = stream_buffer_service.hook_fields_of(session_id, stream_id)
    if await check_interjection(session_id, fields, req.text, incognito=incognito):
        await _refuse_old_home(session_id)  # 等 hook 的期間可能換了家
    # 舊家檢查與 hook 都會讓出執行權，串流可能已換代或結束：最後再驗一次。
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
