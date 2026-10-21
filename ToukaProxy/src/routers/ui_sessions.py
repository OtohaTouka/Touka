"""UI 會話與專案管理路由 (UI Sessions & Projects Router).

負責處理前端對話會話的儲存、檢索、家會話設定，以及專案結構與工作區設定管理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Header, Request  # noqa: TC002
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from memory_server.services.mission_manager import MissionManager
from memory_server.tools import get_manager
from pydantic import BaseModel, Field, field_validator, model_validator

from src.config import DEFAULT_WORKSPACE_ROOT
from src.core.exceptions import ErrorCode, ToukaException
from src.models.history import TreeHistory
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.session_persistence import (
    AbsentSessionSnapshot,
    BufferedStreamState,
    FailedResult,
    PresentSessionSnapshot,
    SessionChanges,
    SessionDocument,
    SessionSaveResponse,
    SessionSnapshotResponse,  # noqa: TC001 — FastAPI resolves the return annotation at runtime.
    SessionStreamStateResponse,
    UnknownResult,
)
from src.services.agent_service import agent_service
from src.services.authorization_gateway import (
    can_access_session,
    require_admin_authority,
    require_authenticated,
)
from src.services.handover_service import handover_service
from src.services.identity_router import get_verified_identity
from src.services.identity_service import identity_service
from src.services.process_registry import process_registry, terminate_processes
from src.services.resource_manager import resource_manager
from src.services.rhythm_service import rhythm_service
from src.services.stream_buffer_service import stream_buffer_service
from src.utils.async_db import (
    SessionPersistenceError,
    get_async_db,
    get_project_root_path,
    get_system_setting,
    session_service,
    set_system_setting,
)
from src.utils.auth_mode import AuthMode, authorize_auth_mode  # noqa: TC001
from src.utils.history_manager import count_rounds_from_linear, get_linear_messages_from_tree
from src.utils.path_security import validate_no_symlink_escape
from src.utils.task_utils import spawn_logged
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ui", tags=["UI Sessions"])

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.responses import Response


class SessionPersistenceRoute(APIRoute):
    """只收斂新保存入口的驗證錯誤；既有其他 UI 錯誤契約保持。"""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        """把新保存路由的輸入驗證錯誤映射為固定安全錯誤。"""
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise ToukaException(ErrorCode.VALIDATION_ERROR) from None

        return safe_handler


persistence_router = APIRouter(
    prefix="/sessions", tags=["UI Sessions"], route_class=SessionPersistenceRoute
)


# Grandfather Batch-2 fix: session/project/settings management requires authentication
# Require any valid authenticated identity to access session/project data.
_require_authenticated = require_authenticated


# DA4-followup: global system-setting writes require admin.
#
# Any authenticated user could otherwise repoint where the server
# reads project files (workspace root).
_require_admin_role = require_admin_authority


_can_access_session = can_access_session  # ADR-0245 修補：共用授權閘道


def _is_registered_agent(candidate_id: str) -> bool:
    """指派防護的名冊判定：``candidate_id`` 是否為真 agent（soul/custom/group/registry）。

    參數刻意為裸字串而非 AgentIdentity——它是「待判定的候選標籤」（可能是裸模型
    id、根本不是身分），判定結果才決定要不要進入身分防護。save_session 的「不得把
    session 掛到別人 agent 名下」檢查只對真 agent 有意義；裸模型 id（如
    ``gemini-2.5-flash``）是顯示標籤而非身分指派，一律放行——否則 normal 使用者的
    模型對話永遠 403（ui_sessions.db 長年空白的成因之一）。
    名冊不可用時 fail-closed 回 True（當作 agent、維持嚴格防護）。
    """
    try:
        return any(a.get("id") == candidate_id for a in agent_service.get_all_agents())
    except Exception:
        logger.exception("Agent registry unavailable; failing closed for %s", candidate_id)
        return True


async def _can_access_project(caller: AgentIdentity, project_id: str | None) -> bool:
    """DA3-follow-up: READ scope for a project — derived from its sessions.

    Projects have no owner column; a project is owned by whoever owns its
    sessions' agents. admin/Debugger see all; otherwise the caller must own at
    least one agent with a session in the project. Projects with no sessions
    have no derivable owner, so are readable only by admin/Debugger.
    """
    if caller.is_admin_or_debugger():
        return True
    if not project_id:
        return False
    # ADR-0107: explicit human owner takes precedence (owner-scoped access).
    owner = await session_service.get_project_owner(project_id)
    if owner:
        return caller.is_human and caller.agent_id == owner
    # Ownerless (legacy/pre-ADR-0107) → fall back to session-derived ownership.
    agent_ids = await session_service.get_project_agent_ids(project_id)
    return any(_can_access_session(caller, aid) for aid in agent_ids)


async def _can_modify_project(caller: AgentIdentity, project_id: str | None) -> bool:
    """WRITE scope for a project (save/delete).

    Explicit owner (ADR-0107) takes precedence; otherwise session-derived. A
    brand-new (not-yet-persisted) project is creatable by any authenticated
    caller, but an EXISTING ownerless + sessionless project is a legacy/orphaned
    row with no derivable owner → admin/Debugger only.

    E-fix: the old `if not agent_ids: return True` also fired for an existing
    orphan, letting any user modify/DELETE it (and NULL other users' session
    links). It now only allows a not-yet-persisted id (a genuine create).
    """
    if caller.is_admin_or_debugger():
        return True
    if not project_id:
        return True
    owner = await session_service.get_project_owner(project_id)
    if owner:
        return caller.is_human and caller.agent_id == owner
    agent_ids = await session_service.get_project_agent_ids(project_id)
    if agent_ids:
        return any(_can_access_session(caller, aid) for aid in agent_ids)
    # No explicit owner and no sessions: allow only when creating a NEW project
    # (id not yet in the DB). An existing orphan is admin/Debugger only (above).
    return not await session_service.project_exists(project_id)


async def _project_root_allowed(caller: AgentIdentity, root_path: str | None) -> bool:
    """[A-fix] 專案 root_path 必須位於工作區 jail 之內 (path-disclosure 防護).

    save_project 原樣存任意 root_path、get_project_files 直接掃描之，使非 admin 可
    把專案指向主機任一目錄再枚舉其檔案樹。本述詞把專案根目錄收斂在設定的
    workspace_root 之下 (SSOT: resource_manager.is_within_workspace)。

    admin/Debugger 放行 (受信任，且 workspace_root 設定本就由其掌控)——與
    `_can_access_project` / `_can_modify_project` 的 admin/Debugger bypass 一致。
    """
    if caller.is_admin_or_debugger():
        return True
    return await resource_manager.is_within_workspace(root_path or "")


class LegacyExportRequest(BaseModel):
    """舊式對話匯出請求模型。"""

    agent_id: AgentIdentity  # Identity SSOT
    filename: str
    json_content: list[dict]


# Pydantic schema for parsing requests
class SessionModel(BaseModel):
    """會話資料模型，包含對話樹、元數據與關聯 Agent。"""

    id: str
    title: str
    messages: dict[str, Any]
    updatedAt: int
    is_home: bool | None = None
    home_for_agent: AgentIdentity | None = None  # Identity SSOT
    agent_id: AgentIdentity | None = None  # Identity SSOT
    project_id: str | None = None
    active_skills: list[str] = Field(default_factory=list)
    active_mcp_servers: list[str] = Field(default_factory=list)
    context_summary: str | None = None
    revision: str | None = None
    # HITL 模式（hitl-unification H1；ADR-0246 三模式）：AuthSwitcher 的選擇經此持久化到
    # user_sessions.auth_level（工具閘門 ToolLayer 的 SSOT）。None＝本次不變更（upsert
    # 保留舊值、新建落 DB 預設 'Manual'）。只收新名稱；Auto／YOLO 另經 save 的模式授權。
    auth_level: AuthMode | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> SessionModel:
        """將保存專屬型別檢查留在 request validation 邊界，避免原輸入進 error log。"""
        self.to_changes()
        return self

    def to_changes(self) -> SessionChanges:
        """家欄位只由 set_home 改動；auth_level 的 None 表示保留。"""
        fields = self.model_fields_set - {"id", "revision", "is_home", "home_for_agent"}
        if self.auth_level is None:
            fields.discard("auth_level")
        return SessionChanges(**{name: getattr(self, name) for name in fields})

    @field_validator("messages", mode="before")
    @classmethod
    def ensure_tree_format(cls, v: Any) -> Any:
        """沿 TreeHistory 將扁平列表轉成 JSON 樹，既有樹保留原始欄位。"""
        if isinstance(v, list):
            return TreeHistory.from_flat_list(v).model_dump(mode="json", exclude_unset=True)
        TreeHistory.model_validate(v)
        return v

    @field_validator("updatedAt", mode="before")
    @classmethod
    def ensure_int_timestamp(cls, v: Any) -> int:
        """確保更新時間為整數時間戳。"""
        if isinstance(v, int):
            return v
        if isinstance(v, (str, float)):
            with contextlib.suppress(ValueError, TypeError):
                return int(float(v))
        msg = "updatedAt must be a valid integer timestamp"
        raise ValueError(msg)


class ProjectModel(BaseModel):
    """專案元數據模型。"""

    id: str
    name: str
    root_path: str
    description: str | None = ""
    updatedAt: int | None = None

    @field_validator("updatedAt", mode="before")
    @classmethod
    def ensure_int_timestamp(cls, v: Any) -> int:
        """確保專案更新時間為整數時間戳，若無則核發當前時間。"""
        if v is None:
            return int(get_current_epoch() * 1000)
        if isinstance(v, int):
            return v
        with contextlib.suppress(ValueError, TypeError):
            return int(float(v))
        return int(get_current_epoch() * 1000)


class SetHomeRequest(BaseModel):
    """設定會話為家請求模型。"""

    session_id: str
    agent_id: AgentIdentity  # Identity SSOT
    is_home: bool


@router.get("/sessions")
async def list_sessions(
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> list[dict[str, Any]]:
    """Retrieve chat sessions the caller may access, sorted by updatedAt desc."""
    sessions = await session_service.get_all_sessions()
    accessible = []
    for s in sessions:
        if _can_access_session(identity, s.get("agent_id"), s.get("owner_user_id")):
            s.pop("owner_user_id", None)  # internal authz field — not exposed to clients
            s.pop("session_scope_version", None)
            accessible.append(s)
    return accessible


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    snapshot: bool = False,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any] | SessionSnapshotResponse:
    """Retrieve a specific chat session by its ID."""
    try:
        current = await session_service.read_persistence_snapshot(session_id, identity)
    except SessionPersistenceError:
        raise ToukaException(ErrorCode.SESSION_SAVE_FAILED) from None
    session = current.session
    if session is None:
        if snapshot:
            return AbsentSessionSnapshot(exists=False, revision=current.revision, session=None)
        raise ToukaException(ErrorCode.SESSION_NOT_FOUND)
    if not _can_access_session(identity, session.get("agent_id"), session.get("owner_user_id")):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    public = {
        key: value
        for key, value in session.items()
        if key not in {"owner_user_id", "session_scope_version"}
    }
    public["is_home"] = bool(public["is_home"])
    if snapshot:
        return PresentSessionSnapshot(
            exists=True, revision=current.revision, session=SessionDocument(**public)
        )
    return {
        **public,
        "revision": current.revision,
        "round_count": count_rounds_from_linear(get_linear_messages_from_tree(public["messages"])),
    }


@router.post("/sessions/set_home")
async def set_home_session(
    req: SetHomeRequest,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """將特定對話設定為 Agent 的家。每個 Agent 只能有一個家。"""
    # ADR-0245 修補（R-U2-SESSION 第 3 次）：檢查只加不減。
    # R1（復原舊檢查）：呼叫者必須能用 req.agent_id 本身——否則沒有靈魂的使用者可把自己的
    # session 標成 ToukaSoul<別人>／ToukaGroup 再設家（這類標籤無人擁有，is_owner 為否）。
    if not _can_access_session(identity, req.agent_id.agent_id):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    # R2：req.agent_id 必須是正式靈魂 ID（ToukaSoul 前綴、群聊、模型名一律拒，admin 亦同）。
    req_passport = agent_service.identify_agent(req.agent_id.agent_id)
    if (
        not req_passport
        or not req_passport["is_toukasoul"]
        or (req_passport["agent_id"] != req.agent_id.agent_id)
    ):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    # R3：目標 session 必須存在。
    target = await session_service.get_session_by_id(req.session_id)
    if target is None:
        raise ToukaException(ErrorCode.SESSION_NOT_FOUND)
    # R4：綁定經 identify_agent 正規化後必須等於 req.agent_id（去 ToukaSoul 前綴後比對）。
    # R5：呼叫者必須能存取目標 session 本身。兩者皆否即拒（判斷不了就保護）。
    bound = target.get("agent_id")
    bound_passport = agent_service.identify_agent(bound) if bound else None
    bound_norm = bound_passport["agent_id"] if bound_passport else bound
    if bound_norm != req.agent_id.agent_id or not _can_access_session(
        identity, bound, target.get("owner_user_id")
    ):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    success = await session_service.set_home(req.session_id, req.agent_id, req.is_home)
    if not success:
        raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)

    return {
        "status": "success",
        "agent_id": req.agent_id.agent_id,
        "session_id": req.session_id if req.is_home else None,
    }


@persistence_router.post("/{session_id}")
async def save_session(
    session_id: str,
    session: SessionModel,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> SessionSaveResponse:
    """Create or update a specific chat session."""
    if session_id != session.id:
        raise ToukaException(ErrorCode.SESSION_ID_MISMATCH)

    try:
        current = await session_service.read_persistence_snapshot(session_id, identity)
    except SessionPersistenceError:
        return SessionSaveResponse(
            session_id=session_id,
            persistence=UnknownResult(
                status="unknown",
                error_code=ToukaException(ErrorCode.SESSION_SAVE_FAILED).error_id,
            ),
        )
    existing = current.session

    # DA3: must own the existing session (no overwriting others) and the agent
    # being assigned (no reassigning a session to an agent you do not own).
    if existing and not _can_access_session(
        identity, existing.get("agent_id"), existing.get("owner_user_id")
    ):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    if not session.revision or session.revision != current.revision:
        return SessionSaveResponse(
            session_id=session_id,
            persistence=FailedResult(
                status="failed",
                error_code=ToukaException(ErrorCode.SESSION_SAVE_FAILED).error_id,
            ),
        )
    # 指派防護只對「真 agent」執行（_is_registered_agent）：裸模型 id 是顯示標籤，
    # 一律放行——否則 normal 使用者的模型對話永遠 403（owner attribution 白做）。
    new_agent_id = session.agent_id.agent_id if session.agent_id else None
    if (
        new_agent_id
        and _is_registered_agent(new_agent_id)
        and not _can_access_session(identity, new_agent_id)
    ):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    # E-residual: a session may only be (newly) linked to a project the caller can
    # access. Otherwise any user could attach their own session to a victim's
    # ownerless/orphan project_id and gain session-derived modify/delete on it
    # (bypassing the _can_modify_project direct-path fix). Re-saves with an unchanged
    # project_id skip the check. Internal autonomous flows (heartbeat/dispatch) call
    # upsert_session directly and bypass this route, so they are unaffected.
    existing_project_id = existing["project_id"] if existing else None
    project_linked = bool(session.project_id) and session.project_id != existing_project_id
    if project_linked and not await _can_access_project(identity, session.project_id):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    changes = _authorized_changes(session, existing, identity)
    fields = changes.model_fields_set
    target_id = (
        (changes.agent_id.agent_id if changes.agent_id else None)
        if "agent_id" in fields
        else (existing["agent_id"] if existing else None)
    )
    passport = agent_service.identify_agent(target_id) if target_id else None
    target = (
        AgentIdentity(
            agent_id=passport["agent_id"] if passport and passport["is_toukasoul"] else target_id
        )
        if target_id
        else None
    )
    result = await session_service.save_ui_session(
        session.id, session.revision, changes, identity, target
    )
    if result.status != "saved":
        return SessionSaveResponse(session_id=session_id, persistence=result)

    new_agent_id = (
        session.agent_id.agent_id
        if session.agent_id
        else (existing["agent_id"] if existing else None)
    )
    new_project_id = (
        session.project_id
        if "project_id" in fields
        else (existing["project_id"] if existing else None)
    )
    old_agent_id = existing["agent_id"] if existing else None

    if new_agent_id and new_project_id and new_agent_id != old_agent_id:
        # P5d：fire-and-forget 交接檔生成——以 spawn_logged 確保失敗會被記錄而非靜默吞掉
        spawn_logged(
            handover_service.generate_handover_file(
                project_id=new_project_id,
                new_agent_id=new_agent_id,
                old_agent_id=old_agent_id,
            ),
            name=f"handover:{old_agent_id}->{new_agent_id}",
        )

    return SessionSaveResponse(session_id=session.id, persistence=result)


def _authorized_changes(
    session: SessionModel, existing: dict[str, Any] | None, identity: AgentIdentity
) -> SessionChanges:
    """模式授權（ADR-0246）：存檔後是 Auto／YOLO 的對話，只有 admin 或該靈魂的擁有者能寫。

    其他人的寫入照 AGENTS.md §2 降成 Manual。看存檔後的模式與靈魂，所以改掛靈魂也會重驗。
    """
    changes = session.to_changes()
    fields = changes.model_fields_set
    mode = changes.auth_level if "auth_level" in fields else None
    if mode is None:
        mode = existing["auth_level"] if existing else "Manual"
    if "agent_id" in fields:
        agent_id = session.agent_id.agent_id if session.agent_id else None
    else:
        agent_id = existing["agent_id"] if existing else None
    allowed = authorize_auth_mode(
        mode,
        is_admin=identity.is_admin_authority(),
        owns_agent=bool(agent_id) and agent_id in identity.owned_agents,
    )
    if allowed == mode:
        return changes
    logger.warning(
        "🛡️ [Security] %s may not set auth mode %s on agent %s; saving Manual.",
        identity.agent_id,
        mode,
        agent_id,
    )
    return changes.model_copy(update={"auth_level": allowed})


async def _authorized_stream_state(
    session_id: str, identity: AgentIdentity
) -> dict[str, Any] | None:
    """UI 綁 requester；只有無痕免 DB，其餘仍驗當前 owner 與同一串流代。"""
    state = stream_buffer_service.get_state(session_id)
    stream_id = state["stream_id"] if state else None
    if state is not None and state.get("started") is not None:
        if not stream_buffer_service.is_owned_by(session_id, identity):
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
        if stream_buffer_service.persistence_mode(session_id, stream_id) == "incognito":
            return state
        try:
            snapshot = await session_service.read_persistence_snapshot(session_id, identity)
        except SessionPersistenceError:
            raise ToukaException(ErrorCode.SESSION_SAVE_FAILED) from None
        session = snapshot.session
    else:
        session = await session_service.get_session_by_id(session_id)
    if not session or not _can_access_session(
        identity, session.get("agent_id"), session.get("owner_user_id")
    ):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    # DB await 期間任何替換均須重新授權；同 requester 也不能將舊操作套到新串流。
    state = stream_buffer_service.get_state(session_id)
    if (state["stream_id"] if state else None) != stream_id:
        raise ToukaException(ErrorCode.STREAM_NOT_FOUND)
    if (
        state is not None
        and state.get("started") is not None
        and not stream_buffer_service.is_owned_by(session_id, identity)
    ):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    return state


@router.get("/sessions/{session_id}/stream/state", response_model=SessionStreamStateResponse)
async def get_session_stream_state(
    session_id: str,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> SessionStreamStateResponse:
    """取得重新授權後的同代串流狀態。"""
    state = await _authorized_stream_state(session_id, identity)
    return SessionStreamStateResponse(
        state=BufferedStreamState.model_validate(state) if state is not None else None
    )


@router.post("/sessions/{session_id}/stop")
async def stop_session_tasks(
    session_id: str,
    stream_id: str | None = None,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Mark a session to stop all autonomous ReAct tasks."""
    state = await _authorized_stream_state(session_id, identity)
    if state is not None and state.get("started") is not None and state["stream_id"] != stream_id:
        raise ToukaException(ErrorCode.STREAM_NOT_FOUND)
    rhythm_service.interrupt_session(session_id)
    # 硬中斷：除了設軟中斷旗標，真正終止該會話正在跑的子進程（如 bash）。先在事件迴圈上
    # 取出登記，再到執行緒裡逐一殺（Windows taskkill 是同步的，不能卡事件迴圈）。
    procs = process_registry.pop_session(session_id)
    killed = await asyncio.to_thread(terminate_processes, procs, session_id)
    # ADR-0081：取消可恢復串流（producer task + 緩衝終態，喚醒所有訂閱者）
    await stream_buffer_service.cancel(
        session_id, stream_id=state["stream_id"] if state else stream_id
    )
    return {
        "status": "success",
        "session_id": session_id,
        "message": f"Interruption signal sent; terminated {killed} running process(es).",
    }


@router.get("/sessions/{session_id}/stream")
async def resume_session_stream(
    session_id: str,
    starting_after: int = -1,
    stream_id: str | None = None,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> Any:
    """ADR-0081：接回進行中（或 TTL 內已完成）的對話串流——重播＋跟流。

    ``starting_after`` 為已收到的最後 seq（chunk index）；-1 表示從頭重播。
    回應為 SSE，與對話串流同格式，前端以同一套解析重建訊息樹。
    """
    state = await _authorized_stream_state(session_id, identity)
    if state is None or (state.get("started") is not None and state["stream_id"] != stream_id):
        raise ToukaException(ErrorCode.STREAM_NOT_FOUND)
    return StreamingResponse(
        stream_buffer_service.subscribe(
            session_id, starting_after=starting_after, stream_id=state["stream_id"]
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/streams/active")
async def list_active_streams(
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """ADR-0081：列出呼叫者可存取的進行中串流（重整後前端據此接回）。

    逐條套用 _can_access_session 過濾（IDOR 防護，DA3 同款述詞）。
    """
    items: list[dict[str, Any]] = []
    for state in stream_buffer_service.list_active():
        try:
            authorized = await _authorized_stream_state(state["session_id"], identity)
        except ToukaException:
            continue
        if (
            authorized
            and authorized["stream_id"] == state["stream_id"]
            and authorized["status"] == "active"
        ):
            items.append(authorized)
    return {"streams": items}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    if_match: str | None = Header(default=None),
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """Delete a specific chat session."""
    captured_stream = stream_buffer_service.get_state(session_id)
    try:
        current = await session_service.read_persistence_snapshot(session_id, identity)
    except SessionPersistenceError:
        raise ToukaException(ErrorCode.SESSION_SAVE_FAILED) from None
    session = current.session
    if session is None:
        # 沿既有刪除冪等合約；只確認目前不存在，不宣稱先前 DELETE 的提交時點。
        return {"status": "success", "session_id": session_id}
    if not _can_access_session(identity, session.get("agent_id"), session.get("owner_user_id")):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    if session["is_home"]:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    result = await session_service.conditional_delete_session(session_id, if_match or "", identity)
    if result.status != "saved":
        raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)
    if captured_stream is not None:
        stream_buffer_service.retire_persistent(session_id, captured_stream["stream_id"])
    return {"status": "success", "session_id": session_id}


@router.post("/sessions/legacy/export")
async def export_legacy(
    req: LegacyExportRequest,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """把切下的舊對話直接歸檔進該 agent 的舊對話庫（``legacy/`` 受保護，不寫檔）。

    ``filename`` 只是資料庫裡的標籤；回應不含任何實體路徑。
    """
    if not _can_access_session(identity, req.agent_id.agent_id):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    try:
        archived = await asyncio.to_thread(
            get_manager().get_legacy_sync_service().archive_payload,
            req.agent_id,
            req.json_content,
            req.filename,
        )
    except Exception as e:
        logger.exception("Failed to export legacy chat")
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=f"Export failed: {e!s}") from e
    if not archived:
        # 沒有可歸檔的訊息：不能回成功，否則前端會把這段當作已存好而剪掉
        raise ToukaException(ErrorCode.VALIDATION_ERROR)
    logger.info("Archived %d legacy messages for %s", archived, req.agent_id.agent_id)
    return {"status": "success", "archived": archived}


# --- Projects Endpoints ---


@router.get("/projects/list")
async def list_projects(
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> list[dict[str, Any]]:
    """列出呼叫者可存取的專案（依擁有權過濾）。"""
    projects = await session_service.get_all_projects()
    accessible = []
    for project in projects:
        if await _can_access_project(identity, project.get("id")):
            project.pop("owner_user_id", None)  # internal authz field — not exposed to clients
            accessible.append(project)
    return accessible


@router.post("/projects/save")
async def save_project(
    project: ProjectModel,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """儲存或更新專案。"""
    if not await _can_modify_project(identity, project.id):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    # A-fix: 非 admin 不得把專案根目錄指向工作區 jail 之外 (path disclosure 防護)。
    if not await _project_root_allowed(identity, project.root_path):
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override="Project root_path must be inside the workspace root.",
        )
    try:
        async with get_async_db() as db:
            updated_at = project.updatedAt or int(get_current_epoch() * 1000)

            # ADR-0107: attribute new projects to the creating human (JWT sub == agent_id
            # for humans). On update, owner_user_id is preserved (not in the UPDATE set).
            owner_user_id = identity.agent_id if identity.is_human else None
            await db.execute(
                """
                INSERT INTO projects (id, name, root_path, description, owner_user_id, updatedAt)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    root_path=excluded.root_path,
                    description=excluded.description,
                    updatedAt=excluded.updatedAt
            """,
                (
                    project.id,
                    project.name,
                    project.root_path,
                    project.description,
                    owner_user_id,
                    updated_at,
                ),
            )
            await db.commit()
    except Exception as e:
        logger.exception("Failed to save project %s", project.id)
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from e
    else:
        return {"status": "success", "project_id": project.id}


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """刪除專案標記（不影響實體檔案）。"""
    if not await _can_modify_project(identity, project_id):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    try:
        async with get_async_db() as db:
            await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            await db.execute(
                "UPDATE user_sessions SET project_id = NULL WHERE project_id = ?",
                (project_id,),
            )
            await db.commit()
    except Exception as e:
        logger.exception("Failed to delete project %s", project_id)
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from e
    else:
        return {"status": "success", "project_id": project_id}


# --- Project Overview & Files ---


@router.get("/settings/workspace")
async def get_workspace_settings(
    _identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """獲取目前的工作區路徑設定。"""
    root = await get_system_setting("workspace_root", DEFAULT_WORKSPACE_ROOT)
    return {"workspace_root": root}


class WorkspaceSettings(BaseModel):
    """工作區設定模型。"""

    workspace_root: str = Field(..., description="工作區根目錄")


@router.post("/settings/workspace")
async def update_workspace_settings(
    payload: WorkspaceSettings,
    _identity: AgentIdentity = Depends(_require_admin_role),  # noqa: B008
) -> dict[str, Any]:
    """更新工作區路徑設定（DA4-followup：全域系統設定，限 admin）。"""
    new_root = payload.workspace_root
    await set_system_setting("workspace_root", new_root)
    resource_manager.invalidate_cache()
    return {"status": "success", "workspace_root": new_root}


@router.get("/projects/{project_id}/overview")
async def get_project_overview(
    project_id: str,
    identity: AgentIdentity = Depends(get_verified_identity),  # noqa: B008
) -> dict[str, Any]:
    """取得專案概觀：最近紀錄、參與模型、相關會話。

    N10 fix: requires authenticated identity to prevent IDOR attacks.
    Owner/Debugger/admin can access any project; other roles are blocked.
    """
    if not identity:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    if not await _can_access_project(identity, project_id):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    async with get_async_db() as db:
        async with db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)) as cursor:
            project = await cursor.fetchone()
        if not project:
            msg = "Project not found"
            raise ToukaException(ErrorCode.SESSION_NOT_FOUND, detail_msg=msg)

        try:
            async with db.execute(
                "SELECT * FROM project_audit WHERE project_id = ? ORDER BY timestamp DESC LIMIT 5",
                (project_id,),
            ) as cursor:
                audits = await cursor.fetchall()

            async with db.execute(
                "SELECT id, title, agent_id, updatedAt FROM user_sessions "
                "WHERE project_id = ? ORDER BY updatedAt DESC",
                (project_id,),
            ) as cursor:
                sessions = await cursor.fetchall()

            agents = list({s["agent_id"] for s in sessions if s["agent_id"]})

            project_public = {k: v for k, v in dict(project).items() if k != "owner_user_id"}
            return {
                "project": project_public,  # internal authz field owner_user_id stripped
                "recent_audits": [dict(a) for a in audits],
                "sessions": [dict(s) for s in sessions],
                "agents": agents,
            }
        except Exception as e:
            logger.exception("Failed to get project overview for %s", project_id)
            raise ToukaException(ErrorCode.SYSTEM_ERROR) from e


@router.get("/projects/{project_id}/files")
async def get_project_files(
    project_id: str,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """遞迴掃描專案目錄並生成 JSON 格式的檔案樹。"""
    if not await _can_access_project(identity, project_id):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    # 讀取專案根目錄 (SSOT: get_project_root_path，與 project_tools 同源)。
    root_path_raw = await get_project_root_path(project_id)
    if not root_path_raw:
        raise ToukaException(
            ErrorCode.SESSION_NOT_FOUND, detail_override="Project root path not found"
        )

    # A-fix: workspace 包含性檢查 (對 legacy / 非前端寫入的列做縱深防禦，非 admin 受限)。
    if not await _project_root_allowed(identity, root_path_raw):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    root_path_str = await resource_manager.translate_path(root_path_raw)
    root_path = Path(root_path_str)

    if not root_path.exists():
        raise ToukaException(
            ErrorCode.SESSION_NOT_FOUND,
            detail_override=f"Physical path not found: {root_path}",
        )

    def scan_dir(path: Path, relative_prefix: str = "", depth: int = 0) -> list[dict[str, Any]]:
        """遞迴掃描目錄結構，限制最大深度為 3 且過濾排除項目。"""
        if depth > 3:
            return []

        entries = []
        try:
            for entry in path.iterdir():
                # A-fix: 跳過會逃逸出專案根目錄的項目 (symlink 或 Windows junction——
                # is_symlink() 不認 junction，故以 resolve() 後的包含性檢查為準；指向
                # jail 內的合法 symlink 仍保留)。
                if (
                    not validate_no_symlink_escape(entry, root_path)
                    or entry.name.startswith(".")
                    or entry.name in ["node_modules", "__pycache__", "venv", ".git"]
                ):
                    continue

                rel_path = f"{relative_prefix}/{entry.name}" if relative_prefix else entry.name
                if entry.is_dir():
                    entries.append(
                        {
                            "name": entry.name,
                            "type": "directory",
                            "path": rel_path,
                            "children": scan_dir(entry, rel_path, depth + 1),
                        }
                    )
                else:
                    entries.append(
                        {
                            "name": entry.name,
                            "type": "file",
                            "path": rel_path,
                            "size": entry.stat().st_size,
                        }
                    )
        except PermissionError:
            pass

        entries.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))
        return entries

    try:
        tree = scan_dir(root_path)
    except Exception as e:
        logger.exception("Failed to scan files for project %s", project_id)
        raise ToukaException(ErrorCode.SYSTEM_ERROR) from e
    else:
        return {"tree": tree, "root_path": str(root_path)}


@router.get("/mission")
async def get_current_mission(
    request: Request,
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """(UI 輔助端點) 獲取當前 Agent 的任務看板狀態。"""
    target_agent_id = request.headers.get("X-Touka-Soul-ID") or identity.agent_id

    # 權限驗證：
    # 1. 只有當 identity.agent_id == target_agent_id，允許。
    # 2. 如果 target_agent_id == "DispatchAgent"
    #    且 identity 具有 "can_supervise_dispatch" 能力，允許。
    # 3. 如果 identity 為特權身分（人類 admin 或 agent Owner/Debugger），允許。
    is_authorized = (
        identity.agent_id == target_agent_id
        or (
            target_agent_id == "DispatchAgent" and "can_supervise_dispatch" in identity.capabilities
        )
        or identity.is_privileged()
    )
    if not is_authorized:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    # 獲取目標的 Agent 身分物件 (Identity SSOT)
    if identity.agent_id == target_agent_id:
        target_identity = identity
    elif target_agent_id == "DispatchAgent":
        target_identity = AgentIdentity(
            agent_id="DispatchAgent", agent_role="base", capabilities=[]
        )
    else:
        target_identity = identity_service.get_identity(target_agent_id)

    manager = MissionManager()
    mission = await manager.get_current_mission(target_identity)
    if not mission:
        msg = "No active mission"
        raise ToukaException(ErrorCode.SESSION_NOT_FOUND, detail_msg=msg)

    return mission.model_dump()


router.include_router(persistence_router)
