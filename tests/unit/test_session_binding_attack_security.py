"""session 綁定可被改寫的兩條繞過路徑（U2-SESSION 第 3 輪；R-U2-SESSION 第 1 次報告）。

第 2 輪修了「直接帶別人的 session_id」，但 reviewer 找到兩條能改寫 session 綁定的路徑，
綁定一被改，`_session_bound` 就放行，讀寫都能繞過：

- attack_a（HIGH 1）：`can_access_session` 對 Owner 角色的「代理」碰到沒有人類擁有者的
  session（一般模型 session 的 agent_id 是模型名、沒有擁有者）一律放行。攻擊者用
  `X-Agent-ID: Bob` 切成自己的靈魂（代理身分、Owner），帶別人一般模型對話的 session_id，
  回覆就寫進對方 session、綁定被改成 Bob。
- attack_b（HIGH 2）：`connect_mcp_server`／`activate_skill` 寫 session 時不檢查存取或綁定，
  還用 `identity=` 把綁定改成執行中的靈魂。
- attack_c（LOW 3）：沒權限寫回時仍以對方 sid 建串流 buffer、清掉對方 session 的中斷旗標。
- LOW 4：`_persist_assistant_turn` 的存取檢查預設關閉，新呼叫端忘了開就沒檢查。

全部離線、只寫測試沙盒的資料庫。這一批先紅：斷言的是修好後的安全行為。
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from src.adapters.schemas import OpenAIChatRequest
from src.core.exceptions import ToukaException
from src.middleware.layers import monitor as monitor_mod
from src.middleware.layers.monitor import MonitorLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.routers.ui_sessions import (
    SetHomeRequest,
    get_session,
    list_sessions,
    set_home_session,
)
from src.services import agent_service as agent_service_mod
from src.services import authorization_gateway
from src.services import dispatcher as dispatcher_mod
from src.services.authorization_gateway import can_access_session
from src.services.stream_buffer_service import StreamBufferService
from src.tools.builtin import system as system_tools
from src.utils.async_db import init_db_async, session_service
from src.utils.history_manager import TreeMessageManager

PLAIN = "rus-plain-model"
VICTIM_USER = "rus-victim-user"
BOB = "RusBob"
ALICE = "RusAlice"
REPLY = "ATTACKER-REPLY"

# X-Agent-ID 代打後的執行身分：擁有者切成自己的靈魂（代理、Owner、非人類）。
BOB_AGENT = AgentIdentity.for_role(BOB, "Owner", is_human=False)
ALICE_AGENT = AgentIdentity.for_role(ALICE, "Owner", is_human=False)
ADMIN_HUMAN = AgentIdentity.for_role("rus-admin", "admin", is_human=True)
OWNER_HUMAN = AgentIdentity.for_role("rus-owner", "normal", is_human=True, owned_agents=[BOB])


@pytest.fixture
async def sandbox_db(tmp_path: Any) -> Any:
    """隔離的 user_sessions 測試 DB（沿用既有 get_db_path patch 範式）。"""
    db_path = tmp_path / "test.db"
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        yield db_path


@pytest.fixture
def fresh_buffer(monkeypatch: pytest.MonkeyPatch) -> StreamBufferService:
    svc = StreamBufferService()
    monkeypatch.setattr(dispatcher_mod, "stream_buffer_service", svc)
    return svc


async def _plain_session(owner: str) -> str:
    sid = f"rus-plain-{uuid.uuid4().hex[:8]}"
    await session_service.upsert_session(
        sid, "victim plain", TreeMessageManager.create_empty_tree(),
        identity=AgentIdentity(agent_id=PLAIN), owner_user_id=owner,
    )
    return sid


async def _soul_session(soul: str) -> str:
    sid = f"rus-soul-{uuid.uuid4().hex[:8]}"
    await session_service.upsert_session(
        sid, "soul session", TreeMessageManager.create_empty_tree(),
        identity=AgentIdentity(agent_id=soul),
    )
    return sid


# --------------------------------------------------------------------------- #
# attack_a（HIGH 1）：Owner 代理不能寫沒有擁有者的別人 session
# --------------------------------------------------------------------------- #


def test_can_access_session_denies_owner_agent_on_ownerless_session() -> None:
    """Owner 角色的代理碰到沒有人類擁有者的一般模型 session → 不得放行。"""
    assert can_access_session(BOB_AGENT, PLAIN, VICTIM_USER) is False
    assert can_access_session(BOB_AGENT, PLAIN, None) is False


async def test_assumed_soul_agent_cannot_write_plain_victim_session(sandbox_db: Any) -> None:
    """X-Agent-ID 代打成 Bob、帶別人一般模型 session 的 id → 不寫回、綁定不被改成 Bob。"""
    sid = await _plain_session(VICTIM_USER)
    written = await dispatcher_mod._persist_assistant_turn(
        sid, REPLY, PLAIN, BOB_AGENT, check_access=True
    )
    assert written is False
    after = await session_service.get_session_by_id(sid)
    assert after["agent_id"] == PLAIN
    assert REPLY not in TreeMessageManager.get_linear_path(after["messages"]).__str__()


async def test_persist_access_check_defaults_to_on(sandbox_db: Any) -> None:
    """LOW 4：沒帶 check_access 也要擋（預設開啟）——Owner 代理寫別人的 session 不成功。"""
    sid = await _plain_session(VICTIM_USER)
    written = await dispatcher_mod._persist_assistant_turn(sid, REPLY, PLAIN, BOB_AGENT)
    assert written is False
    after = await session_service.get_session_by_id(sid)
    assert REPLY not in TreeMessageManager.get_linear_path(after["messages"]).__str__()


# --------------------------------------------------------------------------- #
# attack_b（HIGH 2）：connect_mcp_server／activate_skill 不能改綁定或寫別人 session
# --------------------------------------------------------------------------- #


@pytest.fixture
def skill_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_tools.skill_service, "get_skill", lambda _sid: {"id": _sid})
    monkeypatch.setattr(system_tools.permission_service, "can_use_skill", lambda *_a: True)


@pytest.fixture
def mcp_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_tools.config_utils, "config_exists", lambda _n: True)
    monkeypatch.setattr(
        system_tools.config_utils,
        "read_config_json",
        lambda _n: {"mcpServers": {"srv1": {"enabled": True}}},
    )


def _ctx(human: str | None) -> dict[str, Any]:
    return {"human_principal_id": human, "identity": BOB_AGENT}


async def test_activate_skill_refuses_when_session_bound_to_another_agent(
    skill_seam: None, sandbox_db: Any
) -> None:
    """Bob 帶 Alice 的 session 啟用技能 → 拒絕；Alice session 的綁定與技能清單不變。"""
    sid = await _soul_session(ALICE)
    result = await system_tools.activate_skill(
        "docgen", session_id=sid, identity=BOB_AGENT, agent_id=BOB, _ctx=_ctx(OWNER_HUMAN.agent_id)
    )
    assert "Error" in result or "error" in result
    after = await session_service.get_session_by_id(sid)
    assert after["agent_id"] == ALICE
    assert after.get("active_skills") in (None, [])


async def test_connect_mcp_refuses_when_session_bound_to_another_agent(
    mcp_seam: None, sandbox_db: Any
) -> None:
    """Bob 帶 Alice 的 session 掛 MCP → 拒絕；Alice session 的綁定與 MCP 清單不變。"""
    sid = await _soul_session(ALICE)
    result = await system_tools.connect_mcp_server(
        "srv1", session_id=sid, identity=BOB_AGENT, agent_id=BOB, _ctx=_ctx(OWNER_HUMAN.agent_id)
    )
    assert "Error" in result or "error" in result
    after = await session_service.get_session_by_id(sid)
    assert after["agent_id"] == ALICE
    assert after.get("active_mcp_servers") in (None, [])


async def test_activate_skill_on_own_session_still_works(skill_seam: None, sandbox_db: Any) -> None:
    """Bob 在 Bob 自己的 session 啟用技能 → 照舊成功，綁定維持 Bob。"""
    sid = await _soul_session(BOB)
    result = await system_tools.activate_skill(
        "docgen", session_id=sid, identity=BOB_AGENT, agent_id=BOB, _ctx=_ctx(OWNER_HUMAN.agent_id)
    )
    assert "Success" in result
    after = await session_service.get_session_by_id(sid)
    assert after["agent_id"] == BOB
    assert "docgen" in (after.get("active_skills") or [])


async def test_connect_mcp_on_own_session_still_works(mcp_seam: None, sandbox_db: Any) -> None:
    """Bob 在 Bob 自己的 session 掛 MCP → 照舊成功。"""
    sid = await _soul_session(BOB)
    result = await system_tools.connect_mcp_server(
        "srv1", session_id=sid, identity=BOB_AGENT, agent_id=BOB, _ctx=_ctx(OWNER_HUMAN.agent_id)
    )
    assert "Success" in result
    after = await session_service.get_session_by_id(sid)
    assert "srv1" in (after.get("active_mcp_servers") or [])


# --------------------------------------------------------------------------- #
# attack_c（LOW 3）：沒權限寫回時不要用對方 sid 建 buffer
# --------------------------------------------------------------------------- #


async def test_unauthorized_plain_write_leaves_no_buffer(
    monkeypatch: pytest.MonkeyPatch, sandbox_db: Any, fresh_buffer: StreamBufferService
) -> None:
    """Owner 代理帶別人一般模型 session 送串流 → 不以該 sid 建 buffer、不寫回。"""
    sid = await _plain_session(VICTIM_USER)

    async def _fake_stream(*_a: Any, **_k: Any) -> Any:
        async def _gen() -> Any:
            yield f'data: {{"choices": [{{"delta": {{"content": "{REPLY}"}}}}]}}\n\n'
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr(dispatcher_mod, "get_stream_generator", _fake_stream)
    monkeypatch.setattr(dispatcher_mod, "_maybe_trim_after_chat", AsyncMock())
    request = OpenAIChatRequest(
        model=PLAIN, messages=[{"role": "user", "content": "hi"}], stream=True
    )
    ctx = PipelineContext(session_id=sid, identity=BOB_AGENT)
    ctx["resumable_stream"] = True
    ctx["human_principal_id"] = OWNER_HUMAN.agent_id

    resp = await dispatcher_mod.dispatch_request(PLAIN, request, BOB_AGENT, "rus-req", 0.0, ctx)
    _ = [c async for c in resp.body_iterator]

    after = await session_service.get_session_by_id(sid)
    assert REPLY not in TreeMessageManager.get_linear_path(after["messages"]).__str__()
    assert fresh_buffer.get_state(sid) is None


# --------------------------------------------------------------------------- #
# 正向回歸（協調者條件 1）：靈魂自己、心跳、admin、列自己的 session
# --------------------------------------------------------------------------- #


def test_can_access_session_allows_agent_on_its_own_session() -> None:
    """靈魂／心跳：代理寫自己綁定的 session（agent == session agent）→ 放行。"""
    assert can_access_session(ALICE_AGENT, ALICE, None) is True


def test_can_access_session_allows_admin() -> None:
    assert can_access_session(ADMIN_HUMAN, PLAIN, VICTIM_USER) is True


def test_can_access_session_allows_owner_human_on_own_plain_session() -> None:
    """路由列出自己的一般模型 session：owner 本人（owner_user_id）放行。"""
    assert can_access_session(OWNER_HUMAN, PLAIN, OWNER_HUMAN.agent_id) is True


def test_can_access_session_denies_owner_human_on_someone_elses_plain_session() -> None:
    assert can_access_session(OWNER_HUMAN, PLAIN, VICTIM_USER) is False


# --------------------------------------------------------------------------- #
# attack_d（HIGH）：set_home 不能把別人的 session 設成自己 agent 的家
# --------------------------------------------------------------------------- #


@pytest.fixture
def bob_alice_souls(monkeypatch: pytest.MonkeyPatch) -> None:
    """沙盒沒有真的靈魂目錄：讓 BOB／ALICE 被 identify_agent 認成正式靈魂（R2 前提）。"""

    def _identify(aid: str) -> dict[str, Any] | None:
        norm = aid[9:] if aid.startswith("ToukaSoul") else aid
        if norm in {BOB, ALICE}:
            return {"agent_id": norm, "is_toukasoul": True, "is_custom": False}
        return None

    monkeypatch.setattr(agent_service_mod.agent_service, "identify_agent", _identify)


async def test_set_home_rejects_binding_to_another_agents_session(
    sandbox_db: Any, bob_alice_souls: None
) -> None:
    """X-Agent-ID 代打成 Bob，把綁在 Alice 的 session 設成 Bob 的家 → 403，家不變。"""
    sid = await _soul_session(ALICE)
    req = SetHomeRequest(session_id=sid, agent_id=BOB, is_home=True)
    raised: ToukaException | None = None
    try:
        await set_home_session(req, identity=BOB_AGENT)
    except ToukaException as exc:
        raised = exc
    assert raised is not None and raised.detail["code"] == "AUTH_PERMISSION_DENIED"
    after = await session_service.get_session_by_id(sid)
    assert after.get("home_for_agent") != BOB
    assert bool(after.get("is_home")) is False


async def test_set_home_on_own_session_still_works(
    sandbox_db: Any, bob_alice_souls: None
) -> None:
    """Bob 把綁在 Bob 的 session 設成 Bob 的家 → 成功。"""
    sid = await _soul_session(BOB)
    result = await set_home_session(
        SetHomeRequest(session_id=sid, agent_id=BOB, is_home=True), identity=BOB_AGENT
    )
    assert result["status"] == "success"
    after = await session_service.get_session_by_id(sid)
    assert after.get("home_for_agent") == BOB
    assert bool(after.get("is_home")) is True


# --------------------------------------------------------------------------- #
# 心跳（正向測試）
# --------------------------------------------------------------------------- #


async def test_heartbeat_writes_its_own_soul_session(sandbox_db: Any) -> None:
    """實際心跳寫回範式：以靈魂身分 upsert 自己靈魂的 session → 寫入、綁定維持靈魂。

    真實心跳走 session_service.upsert_session(identity=soul) 直接寫、不經 can_access_session，
    故此條只釘住行為、不要求對閘道突變有反應。
    """
    sid = await _soul_session(BOB)
    tree = TreeMessageManager.append_node(
        TreeMessageManager.create_empty_tree(), "assistant", "HEARTBEAT-REPLY", name=BOB
    )
    ok = await session_service.upsert_session(sid, "soul session", tree, identity=BOB_AGENT)
    assert ok is True
    after = await session_service.get_session_by_id(sid)
    assert after["agent_id"] == BOB
    assert "HEARTBEAT-REPLY" in TreeMessageManager.get_linear_path(after["messages"]).__str__()


async def test_heartbeat_home_chat_tool_write_passes_guard(
    skill_seam: None, sandbox_db: Any
) -> None:
    """心跳家對話（ctx heartbeat_run）裡呼叫 activate_skill，session 綁在靈魂 → 通過守衛。"""
    sid = await _soul_session(BOB)
    ctx = {"human_principal_id": None, "identity": BOB_AGENT, "heartbeat_run": True}
    result = await system_tools.activate_skill(
        "docgen", session_id=sid, identity=BOB_AGENT, agent_id=BOB, _ctx=ctx
    )
    assert "Success" in result
    after = await session_service.get_session_by_id(sid)
    assert "docgen" in (after.get("active_skills") or [])


# --------------------------------------------------------------------------- #
# 路由列表（正向測試）：GET /v1/ui/sessions 只列自己的、明細 200/403
# --------------------------------------------------------------------------- #

MINE_PLAIN_MODEL = "rus-list-plain"
MY_SOUL = "RusMine"
OTHER_SOUL = "RusOther"
ME = "rus-me"
OTHER = "rus-other"

_L_OWNER = AgentIdentity.for_role(ME, "normal", is_human=True, owned_agents=[MY_SOUL])
_L_ADMIN = AgentIdentity.for_role("rus-list-admin", "admin", is_human=True)
_L_ADMIN_ASSUME = AgentIdentity(agent_id=MY_SOUL, human_role="admin", is_human=False)
_L_SOUL = AgentIdentity.for_role(MY_SOUL, "Owner", is_human=False)


def _owns(target: AgentIdentity, caller: AgentIdentity) -> bool:
    return caller.is_admin_authority() or target.agent_id in caller.owned_agents


@pytest.fixture
def owns_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authorization_gateway.resource_manager, "is_owner", _owns)


async def _seed_list_sessions() -> dict[str, str]:
    ids = {
        "mine_plain": "rus-l-mineplain",
        "mine_soul": "rus-l-minesoul",
        "other_plain": "rus-l-otherplain",
        "other_soul": "rus-l-othersoul",
    }
    empty = TreeMessageManager.create_empty_tree
    await session_service.upsert_session(
        ids["mine_plain"], "mp", empty(),
        identity=AgentIdentity(agent_id=MINE_PLAIN_MODEL), owner_user_id=ME,
    )
    await session_service.upsert_session(
        ids["mine_soul"], "ms", empty(), identity=AgentIdentity(agent_id=MY_SOUL)
    )
    await session_service.upsert_session(
        ids["other_plain"], "op", empty(),
        identity=AgentIdentity(agent_id=MINE_PLAIN_MODEL), owner_user_id=OTHER,
    )
    await session_service.upsert_session(
        ids["other_soul"], "os", empty(), identity=AgentIdentity(agent_id=OTHER_SOUL)
    )
    return ids


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("_L_OWNER", {"mine_plain", "mine_soul"}),
        ("_L_ADMIN", {"mine_plain", "mine_soul", "other_plain", "other_soul"}),
        ("_L_ADMIN_ASSUME", {"mine_plain", "mine_soul", "other_plain", "other_soul"}),
        ("_L_SOUL", {"mine_soul"}),
    ],
)
async def test_list_sessions_returns_exactly_my_sessions(
    caller: str, expected: set[str], owns_patch: None, sandbox_db: Any
) -> None:
    ids = await _seed_list_sessions()
    identity = {"_L_OWNER": _L_OWNER, "_L_ADMIN": _L_ADMIN, "_L_ADMIN_ASSUME": _L_ADMIN_ASSUME,
                "_L_SOUL": _L_SOUL}[caller]
    sessions = await list_sessions(identity=identity)
    returned = {s["id"] for s in sessions if s["id"] in ids.values()}
    assert returned == {ids[k] for k in expected}


async def test_session_detail_own_200_other_403(owns_patch: None, sandbox_db: Any) -> None:
    ids = await _seed_list_sessions()
    own = await get_session(ids["mine_soul"], identity=_L_SOUL)
    assert own["id"] == ids["mine_soul"]
    raised: ToukaException | None = None
    try:
        await get_session(ids["other_soul"], identity=_L_SOUL)
    except ToukaException as exc:
        raised = exc
    assert raised is not None and raised.detail["code"] == "AUTH_PERMISSION_DENIED"


# --------------------------------------------------------------------------- #
# LOW：MonitorLayer 清中斷旗標移到授權判定之後（被 403 的請求不再清）
# --------------------------------------------------------------------------- #


async def test_interrupt_flag_not_cleared_when_soul_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非擁有者對別人的靈魂發話 → 403，且不清該 session 的中斷旗標；擁有者 → 放行且有清。"""
    monkeypatch.setattr(
        monitor_mod.agent_service,
        "identify_agent",
        lambda _id: {"agent_id": BOB, "is_toukasoul": True, "is_custom": False},
    )
    cleared: list[str] = []
    monkeypatch.setattr(monitor_mod.rhythm_service, "clear_interrupt", cleared.append)

    class _Req:
        model = BOB
        session_id = "rus-int-sid"

    stranger = AgentIdentity.for_role("rus-stranger", "normal", is_human=True, owned_agents=[])
    ctx = PipelineContext(identity=stranger)
    ctx["headers"] = {}
    raised: ToukaException | None = None
    try:
        await MonitorLayer().process(_Req(), ctx)
    except ToukaException as exc:
        raised = exc
    assert raised is not None and raised.detail["code"] == "AUTH_PERMISSION_DENIED"
    assert cleared == []  # 被 403，旗標未被清

    owner = AgentIdentity.for_role("rus-bob-owner", "normal", is_human=True, owned_agents=[BOB])
    monkeypatch.setattr(
        monitor_mod.agent_service, "resolve_role_and_caps", lambda _id: ("Owner", [])
    )
    ctx2 = PipelineContext(identity=owner)
    ctx2["headers"] = {}
    await MonitorLayer().process(_Req(), ctx2)
    assert cleared == ["rus-int-sid"]  # 擁有者放行後有清


# --------------------------------------------------------------------------- #
# set_home 綁定對照表（U2-SESSION 第 5 批；R-U2-SESSION 第 3 次 HIGH）
#
# req.agent_id 固定為「目標靈魂」TGT。規則（檢查只加不減，相對 7911281b）：
#   R1 呼叫者能用 req.agent_id（can_access_session(identity, req.agent_id)）—— 復原的舊檢查
#   R3 目標 session 存在，否則 404
#   R4 identify_agent 正規化後：綁定 == req.agent_id，否則 403
#   R5 呼叫者能存取目標 session（can_access_session(identity, 綁定, owner_user_id)），否則 403
# 每格寫死 200/403；session 一律存在故無 404。「已有家對話」欄被拒時另驗原家對話沒被動。
# --------------------------------------------------------------------------- #

TGT = "RusTgt"
OTH = "RusOth"
SH_PLAIN = "rus-sh-plain"

_SH_CALLERS = {
    "admin": AgentIdentity.for_role("rus-sh-adm", "admin", is_human=True),
    "owner": AgentIdentity.for_role("rus-sh-own", "normal", is_human=True, owned_agents=[TGT]),
    "nonowner": AgentIdentity.for_role("rus-sh-non", "normal", is_human=True, owned_agents=[OTH]),
    "nosoul": AgentIdentity.for_role("rus-sh-nos", "normal", is_human=True, owned_agents=[]),
    "soul_self": AgentIdentity.for_role(TGT, "Owner", is_human=False),
    "debug": AgentIdentity.for_role("DebugAgent", "Debugger", is_human=False),
    "other_agent": AgentIdentity.for_role(OTH, "Owner", is_human=False),
}

# caller            unbound target other plain prefix
_SH_MATRIX = [
    ("admin",        403,    200,   403,  403,  200),
    ("owner",        403,    200,   403,  403,  200),
    ("nonowner",     403,    403,   403,  403,  403),
    ("nosoul",       403,    403,   403,  403,  403),
    ("soul_self",    403,    200,   403,  403,  403),
    ("debug",        403,    200,   403,  403,  200),
    ("other_agent",  403,    403,   403,  403,  403),
]
_SH_STATES = ["unbound", "target", "other", "plain", "prefix"]
_SH_BINDING = {
    "unbound": None,
    "target": TGT,
    "other": OTH,
    "plain": SH_PLAIN,
    "prefix": "ToukaSoul" + TGT,
}


def _sh_identify(aid: str) -> dict[str, Any] | None:
    norm = aid[9:] if aid.startswith("ToukaSoul") else aid
    if norm in {TGT, OTH}:
        return {"agent_id": norm, "is_toukasoul": True, "is_custom": False}
    return None


def _sh_owns(target: AgentIdentity, caller: AgentIdentity) -> bool:
    return caller.is_admin_authority() or target.agent_id in caller.owned_agents


@pytest.fixture
def set_home_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authorization_gateway.resource_manager, "is_owner", _sh_owns)
    monkeypatch.setattr(agent_service_mod.agent_service, "identify_agent", _sh_identify)


async def _seed_sh_session(state: str, owner: str | None) -> str:
    sid = f"rus-sh-{state}-{uuid.uuid4().hex[:8]}"
    binding = _SH_BINDING[state]
    identity = AgentIdentity(agent_id=binding) if binding else None
    await session_service.upsert_session(
        sid, "sh", TreeMessageManager.create_empty_tree(),
        identity=identity, owner_user_id=owner,
    )
    return sid


@pytest.mark.parametrize("home", [False, True], ids=["no_home", "has_home"])
@pytest.mark.parametrize("state", _SH_STATES)
@pytest.mark.parametrize("row", _SH_MATRIX, ids=[r[0] for r in _SH_MATRIX])
async def test_set_home_binding_matrix(
    row: tuple, state: str, home: bool, set_home_seams: None, sandbox_db: Any
) -> None:
    caller_name = row[0]
    expected = row[1 + _SH_STATES.index(state)]
    caller = _SH_CALLERS[caller_name]
    owner_uid = caller.agent_id if caller.is_human else None

    prior_home_sid = None
    if home:
        prior_home_sid = await _seed_sh_session("target", "rus-prior-owner")
        assert await session_service.set_home(prior_home_sid, AgentIdentity(agent_id=TGT), True)

    sid = await _seed_sh_session(state, owner_uid)
    req = SetHomeRequest(session_id=sid, agent_id=TGT, is_home=True)

    if expected == 200:
        result = await set_home_session(req, identity=caller)
        assert result["status"] == "success"
        assert await session_service.get_home_session_id(AgentIdentity(agent_id=TGT)) == sid
    else:
        raised: ToukaException | None = None
        try:
            await set_home_session(req, identity=caller)
        except ToukaException as exc:
            raised = exc
        assert raised is not None and raised.status_code == expected, f"{caller_name}/{state}"
        if home:  # 被拒時原本的家對話不得被清掉或改掉
            home_now = await session_service.get_home_session_id(AgentIdentity(agent_id=TGT))
            assert home_now == prior_home_sid


# --------------------------------------------------------------------------- #
# 兩個標籤紅測試：normal 使用者用非正式標籤設家 → 403（R1 擋）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("label", ["ToukaSoul" + TGT, "ToukaGroup"], ids=["prefix", "toukagroup"])
async def test_set_home_rejects_nonformal_label(
    label: str, set_home_seams: None, sandbox_db: Any
) -> None:
    """沒有靈魂的 normal 使用者，把自己的 session 標成前綴／ToukaGroup 再設家 → 403。"""
    caller = _SH_CALLERS["nosoul"]
    sid = f"rus-sh-label-{uuid.uuid4().hex[:8]}"
    await session_service.upsert_session(
        sid, "sh", TreeMessageManager.create_empty_tree(),
        identity=AgentIdentity(agent_id=label), owner_user_id=caller.agent_id,
    )
    req = SetHomeRequest(session_id=sid, agent_id=label, is_home=True)
    raised: ToukaException | None = None
    try:
        await set_home_session(req, identity=caller)
    except ToukaException as exc:
        raised = exc
    assert raised is not None and raised.status_code == 403
    assert await session_service.get_home_session_id(AgentIdentity(agent_id=label)) is None


@pytest.mark.parametrize("label", ["ToukaSoul" + TGT, "ToukaGroup"], ids=["prefix", "toukagroup"])
async def test_set_home_rejects_nonformal_label_even_for_admin(
    label: str, set_home_seams: None, sandbox_db: Any
) -> None:
    """R2：req.agent_id 必須是正式靈魂 ID——admin 也不能對前綴／ToukaGroup 標籤設家。"""
    caller = _SH_CALLERS["admin"]
    sid = f"rus-sh-admlabel-{uuid.uuid4().hex[:8]}"
    await session_service.upsert_session(
        sid, "sh", TreeMessageManager.create_empty_tree(),
        identity=AgentIdentity(agent_id=label), owner_user_id=caller.agent_id,
    )
    raised: ToukaException | None = None
    try:
        await set_home_session(
            SetHomeRequest(session_id=sid, agent_id=label, is_home=True), identity=caller
        )
    except ToukaException as exc:
        raised = exc
    assert raised is not None and raised.status_code == 403
    assert await session_service.get_home_session_id(AgentIdentity(agent_id=label)) is None
