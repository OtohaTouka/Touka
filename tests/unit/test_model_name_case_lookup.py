"""ADR-0250 A5：模型名查表不分大小寫（甲＋三條保護；保護 1 在這裡，保護 2、3 見撞名測試）。

唯一查表入口 ``ModelManager.resolve_custom_route``：同一份快照的啟用＋已知自訂連線裡，先找寫法
完全相同的；沒有才不分大小寫找，只有一個符合才用（``original_name`` 帶原名）；兩個以上 409
``MODEL_NAME_AMBIGUOUS``，不挑。``request_model_name`` 把靈魂存的寫法轉成請求用的名字：自訂連線
回原名，其他轉小寫（官方 ID 照舊）。靈魂層、群聊、子代理、心跳都經這個入口。

另補延後清單兩項：決策表第 9 列（模糊名存檔 409、都不寫），以及大寫自訂連線模型存成主要模型
後，聊天送出原名。全部只改沙盒；自訂連線用假的路由快照，不連任何廠商。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.core.exceptions import ToukaException
from src.main import app
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.routers import heartbeat
from src.services.auth_service import AuthService
from src.services.dispatchers import group
from src.services.dispatchers.route_family import is_selectable_brain
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.models.routes import (
    CustomModelRoute,
    CustomRouteDisposition,
    ModelRouteSnapshot,
    ModelRouteSnapshotState,
)
from src.services.soul_service import SoulService
from src.tools import subagent_tools
from src.utils.async_db import SessionSnapshot
from src.utils.brain_persistence import get_active_brain, set_active_brain

AGENT_ID = "DebugAgent"
_STEPUP_PW = "CaseLookupPass123!"
_CHAT = "/v1/chat/completions"
_CHAT_BODY = {"model": AGENT_ID, "messages": [{"role": "user", "content": "hi"}]}
QWEN = "Qwen/Qwen2.5"
UPPER = "Qwen/Qwen2.5-72B-Instruct"


def _route(connection: str) -> CustomModelRoute:
    return CustomModelRoute(
        connection_id=connection, endpoint="http://127.0.0.1:1", api_key="", provider="X"
    )


ROUTES = {name: _route(name) for name in (QWEN, UPPER, "Foo", "foo")}


def _use_snapshot(
    monkeypatch: pytest.MonkeyPatch, active: tuple[str, ...], known: tuple[str, ...] = ()
) -> None:
    snapshot = ModelRouteSnapshot.create(
        generation=1,
        config_fingerprint="sandbox",
        active_routes={name: ROUTES[name] for name in active},
        known_custom_model_ids=frozenset(known),
        state=ModelRouteSnapshotState.READY,
    )
    monkeypatch.setattr(model_manager, "_route_snapshot", snapshot)


# ── 保護 1：plan §3.2 表逐列 ────────────────────────────────────────────────

_ACTIVE = CustomRouteDisposition.ACTIVE_CUSTOM
_INACTIVE = CustomRouteDisposition.KNOWN_CUSTOM_INACTIVE
_UNKNOWN = CustomRouteDisposition.UNKNOWN


@pytest.mark.parametrize(
    ("active", "known", "query", "disposition", "original"),
    [
        ((QWEN,), (), QWEN, _ACTIVE, QWEN),  # 第 1 列：完全相同
        ((QWEN,), (), "qwen/qwen2.5", _ACTIVE, QWEN),  # 第 2 列：只有一個不分大小寫符合
        (("Foo", "foo"), (), "Foo", _ACTIVE, "Foo"),  # 第 3 列：完全相同優先
        (("Foo", "foo"), (), "foo", _ACTIVE, "foo"),  # 第 4 列：完全相同優先
        ((), ("Foo",), "foo", _INACTIVE, "Foo"),  # 第 6 列：停用照舊
        ((), (), "claude-x", _UNKNOWN, None),  # 第 7 列：不是自訂連線
    ],
    ids=["row1-exact", "row2-one-match", "row3-Foo", "row4-foo", "row6-inactive", "row7-none"],
)
def test_resolve_custom_route_table(
    monkeypatch: pytest.MonkeyPatch,
    active: tuple[str, ...],
    known: tuple[str, ...],
    query: str,
    disposition: CustomRouteDisposition,
    original: str | None,
) -> None:
    _use_snapshot(monkeypatch, active, known)
    resolution = model_manager.resolve_custom_route(query)
    assert resolution.disposition is disposition
    assert getattr(resolution, "original_name", "missing") == original
    expected_route = ROUTES[original] if disposition is _ACTIVE else None
    assert resolution.route is expected_route


@pytest.mark.parametrize(
    ("active", "known"),
    [(("Foo", "foo"), ()), (("Foo",), ("foo",))],
    ids=["row5-both-active", "row5-active-and-known"],
)
def test_two_case_matches_are_ambiguous_and_never_picked(
    monkeypatch: pytest.MonkeyPatch, active: tuple[str, ...], known: tuple[str, ...]
) -> None:
    """第 5 列：寫法都不同、不分大小寫有兩個 → 409，不挑（比對範圍是啟用 ∪ 已知）。"""
    _use_snapshot(monkeypatch, active, known)
    with pytest.raises(ToukaException) as raised:
        model_manager.resolve_custom_route("FOO")
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "MODEL_NAME_AMBIGUOUS"


@pytest.mark.parametrize(
    ("active", "known", "stored", "sent"),
    [
        ((QWEN,), (), "qwen/qwen2.5", QWEN),
        ((QWEN,), (), QWEN, QWEN),
        ((), ("Foo",), "foo", "Foo"),
        ((), (), "Claude-Sonnet-4-6", "claude-sonnet-4-6"),
    ],
    ids=["custom-lower", "custom-exact", "known-inactive", "official-lowered"],
)
def test_request_model_name(
    monkeypatch: pytest.MonkeyPatch,
    active: tuple[str, ...],
    known: tuple[str, ...],
    stored: str,
    sent: str,
) -> None:
    """自訂連線（啟用或已知）回原名；其他照舊轉小寫。"""
    _use_snapshot(monkeypatch, active, known)
    assert model_manager.request_model_name(stored) == sent


def test_request_model_name_ambiguous_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_snapshot(monkeypatch, ("Foo", "foo"))
    with pytest.raises(ToukaException) as raised:
        model_manager.request_model_name("FOO")
    assert raised.value.detail["code"] == "MODEL_NAME_AMBIGUOUS"


def test_selectable_brain_follows_the_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A1 的 is_selectable_brain 只呼叫 resolve_custom_route：小寫寫法可選、模糊名 409。"""
    _use_snapshot(monkeypatch, (QWEN, "Foo", "foo"))
    assert is_selectable_brain("qwen/qwen2.5", manager=model_manager) is True
    with pytest.raises(ToukaException) as raised:
        is_selectable_brain("FOO", manager=model_manager)
    assert raised.value.detail["code"] == "MODEL_NAME_AMBIGUOUS"


# ── 聊天（SoulLayer）────────────────────────────────────────────────────────


def _soul(sandbox: str) -> Path:
    soul_dir = Path(sandbox) / "soul" / AGENT_ID
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text("ORIGINAL PERSONA", encoding="utf-8")
    (soul_dir / "config.json").write_text(
        '{"is_toukasoul": true, "base_model": "claude-old"}', encoding="utf-8"
    )
    return soul_dir


def _chat(client: TestClient, dispatch: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("src.services.proxy_service.dispatch_request", dispatch)
    return client.post(_CHAT, json=_CHAT_BODY, headers={"X-Touka-Soul-ID": AGENT_ID})


@pytest.mark.parametrize("stored", ["qwen/qwen2.5", QWEN], ids=["lowercase", "original"])
def test_soul_chat_sends_the_connection_original_name(
    auth_client: TestClient,
    global_test_sandbox: str,
    monkeypatch: pytest.MonkeyPatch,
    stored: str,
) -> None:
    """場景 6：靈魂存成 qwen/qwen2.5 或原名 → 請求送給 Qwen/Qwen2.5（原名）。"""
    _soul(global_test_sandbox)
    _use_snapshot(monkeypatch, (QWEN,))
    set_active_brain(AgentIdentity(agent_id=AGENT_ID), stored)
    dispatch = AsyncMock(return_value={"ok": True})
    response = _chat(auth_client, dispatch, monkeypatch)
    assert response.status_code == 200, response.text
    assert dispatch.call_args.args[0] == QWEN


def test_soul_chat_with_ambiguous_brain_is_409_and_not_dispatched(
    auth_client: TestClient, global_test_sandbox: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """場景 7：自訂連線同時有 Foo 和 foo、靈魂設定是 FOO → 409，不會任意挑一個。"""
    _soul(global_test_sandbox)
    _use_snapshot(monkeypatch, ("Foo", "foo"))
    set_active_brain(AgentIdentity(agent_id=AGENT_ID), "FOO")
    dispatch = AsyncMock(return_value={"ok": True})
    response = _chat(auth_client, dispatch, monkeypatch)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MODEL_NAME_AMBIGUOUS"
    dispatch.assert_not_called()


# ── 延後清單：靈魂存檔（人類 admin＋步升密碼）────────────────────────────────


def _human_save(config: dict[str, Any]) -> Any:
    user = AuthService().create_user(f"case-{uuid.uuid4().hex}@test.com", _STEPUP_PW, "admin")
    human = AgentIdentity.for_role(user.id, "admin", is_human=True)
    previous = app.dependency_overrides.get(get_verified_identity)
    app.dependency_overrides[get_verified_identity] = lambda: human
    try:
        return TestClient(app, raise_server_exceptions=False).post(
            f"/v1/souls/{AGENT_ID}",
            json={"config": config, "persona": "NEW PERSONA", "admin_password": _STEPUP_PW},
        )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_verified_identity, None)
        else:
            app.dependency_overrides[get_verified_identity] = previous


def test_saving_an_ambiguous_model_is_409_and_writes_nothing(
    global_test_sandbox: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0250 決策表第 9 列：Foo、foo 都在、新值 FOO → 409，config.json 與 soul.md 都不寫。"""
    soul_dir = _soul(global_test_sandbox)
    _use_snapshot(monkeypatch, ("Foo", "foo"))
    before = {name: (soul_dir / name).read_bytes() for name in ("config.json", "soul.md")}
    response = _human_save({"base_model": "FOO"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MODEL_NAME_AMBIGUOUS"
    assert {name: (soul_dir / name).read_bytes() for name in before} == before
    assert get_active_brain(AgentIdentity(agent_id=AGENT_ID)) is None


def test_saved_uppercase_connection_model_is_chatted_by_its_original_name(
    auth_client: TestClient, global_test_sandbox: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U1 的大寫自訂連線測試的聊天端：存成主要模型後，聊天送出的是原名。"""
    _soul(global_test_sandbox)
    _use_snapshot(monkeypatch, (UPPER,))
    saved = _human_save({"base_model": UPPER})
    assert saved.status_code == 200, saved.text
    assert get_active_brain(AgentIdentity(agent_id=AGENT_ID)) == UPPER
    dispatch = AsyncMock(return_value={"ok": True})
    response = _chat(auth_client, dispatch, monkeypatch)
    assert response.status_code == 200, response.text
    assert dispatch.call_args.args[0] == UPPER


# ── 群聊、子代理、心跳：同一個入口 ──────────────────────────────────────────


async def test_group_member_uses_the_connection_original_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_snapshot(monkeypatch, (QWEN,))
    models: list[str] = []

    async def _fake_stream(model: str, *_args: Any) -> Any:
        models.append(model)

        async def _gen() -> Any:
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr(group, "parse_group_roster", lambda *_a: [{"name": "A", "id": "SoulA"}])
    monkeypatch.setattr(group, "get_active_brain", lambda _identity: "qwen/qwen2.5")
    monkeypatch.setattr(group, "get_stream_generator", _fake_stream)
    monkeypatch.setattr(group, "record_working_brain", lambda *_a: None)
    monkeypatch.setattr(group, "soul_service", MagicMock(inject_home_messages=AsyncMock()))
    request = OpenAIChatRequest(
        model="ToukaGroup", messages=[OpenAIMessage(role="user", content="@A hi")]
    )
    _ = [c async for c in group.dispatch_group_stream(request, "r", 0.0, PipelineContext())]
    assert models == [QWEN]


async def test_subagent_on_parent_brain_uses_the_connection_original_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_snapshot(monkeypatch, (QWEN,))
    parent = AgentIdentity.for_role("CaseParent", "Owner")
    set_active_brain(parent, "qwen/qwen2.5")
    seen: dict[str, Any] = {}

    async def _assemble(**kwargs: Any) -> Any:
        seen["model"] = kwargs["model"]
        return MagicMock(model=kwargs["model"])

    async def _stream(*_args: Any) -> Any:
        async def _gen() -> Any:
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr(subagent_tools, "_assemble_subagent_request", _assemble)
    monkeypatch.setattr(subagent_tools, "get_stream_generator", _stream)
    await subagent_tools._run_subagent(
        goal="g", parent_identity=parent, parent_request_id="r", base_ctx={}, model=None
    )
    assert seen["model"] == QWEN


async def _no_chunks() -> Any:
    for _ in ():
        yield ""


@pytest.mark.parametrize(
    ("override", "stored", "sent"),
    [(None, "qwen/qwen2.5", QWEN), (None, "Claude-Sonnet-4-6", "claude-sonnet-4-6"),
     ("qwen/qwen2.5", None, QWEN)],
    ids=["brain-custom", "brain-official", "override-custom"],
)
async def test_heartbeat_pulse_uses_the_same_entry_as_chat(
    monkeypatch: pytest.MonkeyPatch, override: str | None, stored: str | None, sent: str
) -> None:
    """心跳原本不轉小寫也不認自訂連線的大小寫；改成和聊天一致。"""
    _use_snapshot(monkeypatch, (QWEN,))
    identity = AgentIdentity.for_role(agent_id="CasePulse", role="Owner")
    if stored:
        set_active_brain(identity, stored)
    captured: dict[str, Any] = {}

    async def _dispatch(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(body_iterator=_no_chunks())

    soul = object.__new__(SoulService)  # 不污染 singleton
    soul.get_soul_data = lambda _identity: ("", {})
    session = {"id": "s", "title": "t", "messages": [{"role": "user", "content": "hi"}]}
    monkeypatch.setattr(
        heartbeat, "session_service",
        MagicMock(read_execution_snapshot=AsyncMock(return_value=SessionSnapshot(session, "rev"))),
    )
    monkeypatch.setattr(heartbeat, "dispatch_request", _dispatch)
    monkeypatch.setattr(heartbeat, "autonomy_service", MagicMock())
    monkeypatch.setattr(heartbeat, "rhythm_service", MagicMock())
    monkeypatch.setattr(heartbeat, "soul_service", soul)
    await heartbeat._execute_single_pulse("s", identity, override, PipelineContext(), 1, 0.0, "")
    assert captured["model_name"] == sent
    assert captured["request"].model == sent
