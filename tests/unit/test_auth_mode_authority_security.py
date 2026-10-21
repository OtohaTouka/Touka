"""ADR-0246 模式授權與舊值讀取對照（U2c-4；修 D_permissions §1.9-1）。

舊的授權閘在 MonitorLayer，會被 ToolLayer 用 session 的值覆寫而失效；實際只有 save 的對話
存取檢查，任何能存取對話的人都能把它設成 SafeAuto／FullYOLO。現在：

- 存檔後是 Auto／YOLO 的對話，只有 admin 或該靈魂的擁有者能寫；其他人的寫入照 AGENTS.md
  §2 降成 Manual（看存檔後的模式與靈魂：沒帶 auth_level 的存檔也一樣）。
- 寫入與 API 輸入只收新名稱；已存的舊值讀取時對照（ReadOnly、SafeAuto、看不懂 → Manual，
  FullYOLO → YOLO），存檔時自然寫回新名稱。不做資料庫遷移。

全走 conftest 沙箱（TestClient＋沙箱 SQLite）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from src.adapters.schemas import OpenAIChatRequest
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.middleware.layers.monitor import MonitorLayer
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.routers import ui_sessions as ui_sessions_module
from src.services.agent_service import agent_service
from src.services.identity_router import get_verified_identity
from src.utils.async_db import get_async_db, session_service

SOUL = "AuthModeSoulB"
OTHER_SOUL = "AuthModeSoulA"
BARE_MODEL = "authmode-bare-model"

_IDENTITIES = {
    "admin": AgentIdentity.for_role("authmode-admin", "admin", is_human=True),
    "owner": AgentIdentity.for_role(
        "authmode-owner", "normal", is_human=True, owned_agents=[SOUL]
    ),
    "other_soul_owner": AgentIdentity.for_role(
        "authmode-other", "normal", is_human=True, owned_agents=[OTHER_SOUL]
    ),
    "normal": AgentIdentity.for_role("authmode-normal", "normal", is_human=True),
    # 同一個人類，後來不再擁有 SOUL（仍是該對話的 owner_user_id，存取得到）
    "former_owner": AgentIdentity.for_role("authmode-owner", "normal", is_human=True),
}


class _Caller:
    identity: AgentIdentity = _IDENTITIES["admin"]


def _owns(target: AgentIdentity, caller: AgentIdentity) -> bool:
    """沙箱沒有 users.json 帳號：擁有權照身分的 owned_agents（與登入時同源）。"""
    return caller.is_admin_authority() or target.agent_id in caller.owned_agents


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        ui_sessions_module, "_is_registered_agent", lambda c: c in {SOUL, OTHER_SOUL}
    )
    monkeypatch.setattr(ui_sessions_module.resource_manager, "is_owner", _owns)
    _Caller.identity = _IDENTITIES["admin"]
    app.dependency_overrides[get_verified_identity] = lambda: _Caller.identity
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _save(client: TestClient, who: str, sid: str, **fields):
    _Caller.identity = _IDENTITIES[who]
    snapshot = client.get(f"/v1/ui/sessions/{sid}?snapshot=true")
    revision = snapshot.json()["revision"] if snapshot.status_code == 200 else "missing"
    payload = {
        "revision": revision,
        "id": sid,
        "title": "auth-mode probe",
        "messages": [],
        "updatedAt": 1760000000000,
        **fields,
    }
    return client.post(f"/v1/ui/sessions/{sid}", json=payload)


async def _stored_mode(sid: str) -> str | None:
    async with get_async_db() as db, db.execute(
        "SELECT auth_level FROM user_sessions WHERE id = ?", (sid,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else row["auth_level"]


# ── 四種身分寫 Auto／YOLO ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["Auto", "YOLO"])
@pytest.mark.parametrize(
    ("who", "agent_id", "status", "stored"),
    [
        ("admin", SOUL, 200, "same"),
        ("owner", SOUL, 200, "same"),
        ("other_soul_owner", SOUL, 403, None),  # 別人的靈魂：連對話都掛不上去
        ("normal", SOUL, 403, None),
        ("normal", BARE_MODEL, 200, "Manual"),  # 一般使用者：降成 Manual
    ],
)
async def test_auto_modes_need_admin_or_soul_owner(
    client, requested, who, agent_id, status, stored
):
    sid = f"authmode-{who}-{agent_id}-{requested}"
    response = _save(client, who, sid, agent_id=agent_id, auth_level=requested)
    assert response.status_code == status, response.text
    expected = requested if stored == "same" else stored
    assert await _stored_mode(sid) == expected


@pytest.mark.asyncio
async def test_caller_who_no_longer_owns_the_soul_cannot_keep_auto_modes(client):
    sid = "authmode-former-owner"
    assert _save(client, "owner", sid, agent_id=SOUL, auth_level="YOLO").status_code == 200
    assert await _stored_mode(sid) == "YOLO"

    # 仍是 owner_user_id、存取得到，但已不擁有 SOUL：明寫 YOLO → Manual
    assert _save(client, "former_owner", sid, auth_level="YOLO").status_code == 200
    assert await _stored_mode(sid) == "Manual"

    # 擁有者再設回來；之後前擁有者存別的欄位（沒帶 auth_level）也不能讓它留在 YOLO
    assert _save(client, "owner", sid, auth_level="Auto").status_code == 200
    assert await _stored_mode(sid) == "Auto"
    assert _save(client, "former_owner", sid, title="renamed").status_code == 200
    assert await _stored_mode(sid) == "Manual"


@pytest.mark.asyncio
async def test_owner_save_without_auth_level_keeps_the_mode(client):
    sid = "authmode-owner-keeps"
    _save(client, "owner", sid, agent_id=SOUL, auth_level="YOLO")
    assert _save(client, "owner", sid, title="renamed").status_code == 200
    assert await _stored_mode(sid) == "YOLO"


def test_manual_is_always_writable(client):
    sid = "authmode-manual"
    response = _save(client, "normal", sid, agent_id=BARE_MODEL, auth_level="Manual")
    assert response.status_code == 200


# ── 寫入與 API 輸入只收新名稱 ──────────────────────────────────────────────────


@pytest.mark.parametrize("legacy", ["ReadOnly", "SafeAuto", "FullYOLO"])
def test_save_rejects_retired_names(client, legacy):
    response = _save(client, "admin", f"authmode-legacy-{legacy}", auth_level=legacy)
    assert response.status_code == 422


@pytest.mark.parametrize("legacy", ["ReadOnly", "SafeAuto", "FullYOLO"])
def test_debug_execute_rejects_retired_names(client, legacy):
    _Caller.identity = _IDENTITIES["admin"]
    response = client.post(
        "/debug/execute",
        json={"tool_name": "read", "args": {}, "auth_level": legacy},
    )
    assert response.status_code == 422


# ── 已存的舊值：讀取時對照，存檔時自然寫回新名稱 ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "read_as"),
    [
        ("ReadOnly", "Manual"),
        ("SafeAuto", "Manual"),
        ("FullYOLO", "YOLO"),
        ("GodMode", "Manual"),
    ],
)
async def test_legacy_stored_values_are_read_through_the_table(client, stored, read_as):
    sid = f"authmode-stored-{stored}"
    # session 要綁這次執行的 agent，存下的 Auto／YOLO 才會被採用（R-2c）
    saved = _save(client, "admin", sid, agent_id="AuthModeAgent", auth_level="Manual")
    assert saved.status_code == 200
    async with get_async_db() as db:
        await db.execute("UPDATE user_sessions SET auth_level = ? WHERE id = ?", (stored, sid))
        await db.commit()

    snapshot = client.get(f"/v1/ui/sessions/{sid}?snapshot=true")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["session"]["auth_level"] == read_as  # noqa: PAC_MANUAL_LEVEL_CHECK
    listed = {s["id"]: s for s in client.get("/v1/ui/sessions").json()}
    assert listed[sid]["auth_level"] == read_as  # noqa: PAC_MANUAL_LEVEL_CHECK

    ctx = PipelineContext(
        session_id=sid,
        agent_id="AuthModeAgent",
        identity=AgentIdentity.for_role(agent_id="AuthModeAgent", role="Owner", is_human=False),
    )
    request = OpenAIChatRequest(
        model="claude-test-brain", messages=[{"role": "user", "content": "hi"}]
    )
    await ToolLayer().process(request, ctx)
    assert ctx.auth_level == read_as

    # admin 存別的欄位：資料庫寫回新名稱（不做遷移，存檔時自然轉換）
    assert _save(client, "admin", sid, title="renamed").status_code == 200
    assert await _stored_mode(sid) == read_as


@pytest.mark.asyncio
async def test_heartbeat_upsert_rewrites_legacy_value_with_new_name(client):
    sid = "authmode-upsert-legacy"
    assert _save(client, "admin", sid, auth_level="Manual").status_code == 200
    async with get_async_db() as db:
        await db.execute("UPDATE user_sessions SET auth_level = 'FullYOLO' WHERE id = ?", (sid,))
        await db.commit()
    session = await session_service.get_session_by_id(sid)
    assert await session_service.upsert_session(sid, session["title"], session["messages"])
    assert await _stored_mode(sid) == "YOLO"


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "read_as"), [("FullYOLO", "YOLO"), ("SafeAuto", "Manual")])
async def test_tool_layer_maps_legacy_value_itself(monkeypatch, stored, read_as):
    """ToolLayer 自己也過對照表：就算 session 來源沒對照過（替身），模式仍是新名稱。"""
    monkeypatch.setattr(
        "src.utils.async_db.session_service.get_session_by_id",
        AsyncMock(
            return_value={
                "auth_level": stored, "agent_id": "AuthModeAgent", "active_mcp_servers": []
            }
        ),
    )
    ctx = PipelineContext(
        session_id="authmode-tool-layer",
        agent_id="AuthModeAgent",
        identity=AgentIdentity.for_role(agent_id="AuthModeAgent", role="Owner", is_human=False),
    )
    request = OpenAIChatRequest(
        model="claude-test-brain", messages=[{"role": "user", "content": "hi"}]
    )
    await ToolLayer().process(request, ctx)
    assert ctx.auth_level == read_as


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["Auto", "YOLO"])
async def test_incognito_session_without_a_row_runs_manual(client, requested):
    """無痕對話不寫資料庫（ADR-0086）：ToolLayer 查不到這筆就用 Manual（U2c-6 前端據此鎖定）。

    擁有者在請求帶 auth_level、上游也把它放進 ctx，都不生效：模式只來自已存的 session。
    """
    sid = "authmode-incognito-never-saved"
    assert await session_service.get_session_by_id(sid) is None
    ctx = PipelineContext(session_id=sid, agent_id=SOUL, identity=_IDENTITIES["owner"])
    ctx["incognito"] = True
    ctx["auth_level"] = requested
    request = OpenAIChatRequest(
        model="claude-test-brain",
        messages=[{"role": "user", "content": "hi"}],
        session_id=sid,
        incognito=True,
        auth_level=requested,
    )
    await ToolLayer().process(request, ctx)
    assert ctx.auth_level == "Manual"


# ── 代理身分寫模式（R-2c ③ #3）：is_owner 用真的（不替身），看實際授權路徑 ───────────

_AGENT_WRITERS = {
    "soul_itself": (AgentIdentity.for_role(SOUL, "Owner"), 200, "Manual"),
    "debug_agent": (AgentIdentity.for_role("DebugAgent", "Debugger"), 200, None),
    "dispatch_agent": (AgentIdentity.for_role("DispatchAgent", "Base"), 403, None),
    "subagent": (AgentIdentity.for_role("__authmode_sub__", "Subagent"), 403, None),
    # admin 代打靈魂：保留 admin 權（human_role admin），可以存 YOLO
    "admin_assuming_soul": (
        AgentIdentity(agent_id=SOUL, human_role="admin", is_human=False), 200, "YOLO"
    ),
}


@pytest.fixture
def agent_client(monkeypatch):
    monkeypatch.setattr(ui_sessions_module, "_is_registered_agent", lambda c: c == SOUL)
    app.dependency_overrides[get_verified_identity] = lambda: _Caller.identity
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("who", list(_AGENT_WRITERS))
async def test_agent_identities_cannot_set_auto_modes(agent_client, who):
    """只有 admin 權威（含 admin 代打）或擁有者能存 Auto／YOLO：靈魂本身降成 Manual，
    DebugAgent 跨靈魂保存遭歸屬閘拒絕，DispatchAgent 與子代理連對話都掛不上去。"""
    identity, status, stored = _AGENT_WRITERS[who]
    _Caller.identity = identity
    sid = f"authmode-agent-{who}"
    snapshot = agent_client.get(f"/v1/ui/sessions/{sid}?snapshot=true")
    revision = snapshot.json()["revision"] if snapshot.status_code == 200 else "missing"
    response = agent_client.post(
        f"/v1/ui/sessions/{sid}",
        json={"revision": revision, "id": sid, "title": "p", "messages": [],
              "updatedAt": 1760000000000, "agent_id": SOUL, "auth_level": "YOLO"},
    )
    assert response.status_code == status, response.text
    if who == "debug_agent":
        assert response.json()["persistence"] == {
            "status": "failed",
            "error_code": ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH).error_id,
        }
    assert await _stored_mode(sid) == stored


# ── 新對話還沒發第一句就切模式（R-2c ② #1）：前端帶上目前選的靈魂一起存 ────────────


@pytest.mark.asyncio
async def test_new_session_mode_switch_with_selected_soul_keeps_owner_auto(client):
    sid = "authmode-new-session-owner"
    assert _save(client, "owner", sid).status_code == 200  # 開新對話：還沒有 agent_id
    # 舊前端只送模式：沒有靈魂可驗擁有權，降成 Manual
    assert _save(client, "owner", sid, auth_level="Auto").status_code == 200
    assert await _stored_mode(sid) == "Manual"
    # 現在一起帶上目前選的靈魂：擁有者（非 admin）存得下 Auto
    assert _save(client, "owner", sid, agent_id=SOUL, auth_level="Auto").status_code == 200
    assert await _stored_mode(sid) == "Auto"


@pytest.mark.asyncio
async def test_new_session_mode_switch_still_checks_ownership(client):
    sid = "authmode-new-session-normal"
    assert _save(client, "normal", sid).status_code == 200
    assert _save(client, "normal", sid, agent_id=SOUL, auth_level="YOLO").status_code == 403
    response = _save(client, "normal", sid, agent_id=BARE_MODEL, auth_level="YOLO")
    assert response.status_code == 200
    assert await _stored_mode(sid) == "Manual"


# ── get_session_by_id 自己也過對照表（R-2c ② #2）────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "read_as"), [("FullYOLO", "YOLO"), ("SafeAuto", "Manual"), ("ReadOnly", "Manual")]
)
async def test_get_session_by_id_maps_legacy_values(client, stored, read_as):
    sid = f"authmode-by-id-{stored}"
    assert _save(client, "admin", sid, auth_level="Manual").status_code == 200
    async with get_async_db() as db:
        await db.execute("UPDATE user_sessions SET auth_level = ? WHERE id = ?", (stored, sid))
        await db.commit()
    session = await session_service.get_session_by_id(sid)
    mode = session["auth_level"]
    assert mode == read_as


# ── 存下的 Auto／YOLO 只給 session 綁的那個 agent（R-2c ② 第 3 次複審）────────────


async def _tool_layer_mode_for(sid: str, acting_agent: str) -> str | None:
    """ToolLayer 實際採用的模式；ctx agent_id＝前景代打後這次執行的 agent。"""
    ctx = PipelineContext(
        session_id=sid,
        agent_id=acting_agent,
        identity=AgentIdentity.for_role(acting_agent, "Owner", is_human=False),
    )
    request = OpenAIChatRequest(
        model="claude-test-brain", messages=[{"role": "user", "content": "hi"}]
    )
    await ToolLayer().process(request, ctx)
    return ctx.auth_level


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["Auto", "YOLO"])
async def test_saved_auto_mode_only_applies_to_the_bound_agent(client, mode):
    """擁有者照 A 存下 Auto，這次卻執行 B（自己組請求或第一句前換模型）：一律 Manual。"""
    sid = f"authmode-bound-{mode}"
    assert _save(client, "owner", sid, agent_id=SOUL, auth_level=mode).status_code == 200
    assert await _tool_layer_mode_for(sid, SOUL) == mode
    assert await _tool_layer_mode_for(sid, OTHER_SOUL) == "Manual"
    assert await _tool_layer_mode_for(sid, BARE_MODEL) == "Manual"


@pytest.mark.asyncio
async def test_session_without_a_bound_agent_runs_manual(client):
    """判斷不了就保護：session 沒綁 agent 時，存下的 YOLO 不採用。"""
    sid = "authmode-unbound-yolo"
    assert _save(client, "admin", sid, auth_level="YOLO").status_code == 200
    assert await _stored_mode(sid) == "YOLO"
    assert await _tool_layer_mode_for(sid, SOUL) == "Manual"


@pytest.mark.asyncio
async def test_legacy_prefixed_session_agent_is_compared_by_its_canonical_id(client, monkeypatch):
    """舊前綴 ID（ToukaSoulX）經 agent_service 正規化成正式 ID 後再比，不拿原始字串比。"""
    sid = "authmode-legacy-prefix"
    assert _save(client, "admin", sid, agent_id=SOUL, auth_level="Auto").status_code == 200
    async with get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET agent_id = ? WHERE id = ?", ("ToukaSoul" + SOUL, sid)
        )
        await db.commit()

    def _identify(potential_id: str) -> dict[str, str] | None:
        canonical = potential_id.removeprefix("ToukaSoul")
        return {"agent_id": canonical} if canonical in {SOUL, OTHER_SOUL} else None

    monkeypatch.setattr(agent_service, "identify_agent", _identify)
    assert await _tool_layer_mode_for(sid, SOUL) == "Auto"
    assert await _tool_layer_mode_for(sid, OTHER_SOUL) == "Manual"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "acting", "expected"),
    [
        (SOUL, SOUL, "Auto"),
        # 一般模型認不出 agent：MonitorLayer 走 session 連結回到 A（SoulLayer 用 A 的大腦）
        ("plain-model-x", SOUL, "Auto"),
        # 請求點名另一個認得的 agent：這次執行的是 B，A 的 Auto 不採用
        (OTHER_SOUL, OTHER_SOUL, "Manual"),
    ],
)
async def test_monitor_then_tool_layer_uses_the_acting_agent(
    client, monkeypatch, model, acting, expected
):
    sid = f"authmode-pipeline-{model}"
    assert _save(client, "owner", sid, agent_id=SOUL, auth_level="Auto").status_code == 200

    def _identify(potential_id: str) -> dict[str, object] | None:
        known = {SOUL, OTHER_SOUL}
        if potential_id not in known:
            return None
        return {"agent_id": potential_id, "is_toukasoul": True, "is_custom": False}

    monkeypatch.setattr(agent_service, "identify_agent", _identify)
    # ADR-0245 修補後指定別的靈魂要擁有它：這位擁有者兩個都擁有，才走得到「執行的是 B」
    both = AgentIdentity.for_role(
        "authmode-owner", "normal", is_human=True, owned_agents=[SOUL, OTHER_SOUL]
    )
    ctx = PipelineContext(identity=both, agent_id="authmode-owner")
    ctx["headers"] = {"x-touka-session-id": sid}
    request = OpenAIChatRequest(model=model, messages=[{"role": "user", "content": "hi"}])
    await MonitorLayer().process(request, ctx)
    assert ctx.agent_id == acting
    await ToolLayer().process(request, ctx)
    assert ctx.auth_level == expected
