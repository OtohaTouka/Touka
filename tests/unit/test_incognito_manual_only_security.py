"""無痕對話一律 Manual（ADR-0246；人類 10/12：無痕沒辦法追溯做了什麼，嚴格比較安心）。

U2c-6 只證明「無痕沒存檔 → 讀不到 → Manual」。這裡證明是明確強制：不論請求、ctx、同一個
session id 已存的值，或心跳、子代理繼承來的值，無痕一律 Manual。非無痕的行為不變
（擁有者照樣能開 Auto／YOLO）。

全走沙箱：tmp_path SQLite、假的 stream generator，不打任何 API。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from src.adapters.schemas import OpenAIChatRequest
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.session_persistence import IncognitoStreamAdmission
from src.routers.proxy import _apply_chat_request_context
from src.tools import subagent_tools
from src.utils.async_db import init_db_async, session_service
from src.utils.auth_mode import enforce_incognito_manual

SOUL = "IncognitoManualSoul"
OWNER = AgentIdentity.for_role(SOUL, "Owner", is_human=False)
TREE = {"messages": {}, "currentId": None}


@pytest.fixture
async def sandbox_db(tmp_path):
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "incognito.db"):
        await init_db_async()
        yield


async def _tool_layer_mode(*, incognito: bool, mode: str, session_id: str | None = None,
                           **flags: Any) -> str | None:
    """跑 ToolLayer，回傳實際模式；請求與 ctx 都帶 ``mode``。"""
    ctx = PipelineContext(
        session_id=session_id, agent_id=SOUL, identity=OWNER, auth_level=mode, incognito=incognito
    )
    for key, value in flags.items():
        ctx[key] = value
    request = OpenAIChatRequest(
        model="claude-test-brain",
        messages=[{"role": "user", "content": "hi"}],
        session_id=session_id,
        incognito=incognito,
        auth_level=mode,
    )
    await ToolLayer().process(request, ctx)
    return ctx.auth_level


async def _save_row(sid: str, mode: str) -> None:
    # 綁這次執行的靈魂：存下的 Auto／YOLO 只給 session 綁的 agent（ADR-0246 R-2c）
    assert await session_service.upsert_session(sid, "saved", TREE, OWNER, auth_level=mode)
    row = await session_service.get_session_by_id(sid)
    assert row is not None
    stored = row["auth_level"]
    assert stored == mode


@pytest.mark.parametrize(
    ("value", "incognito", "expected"),
    [
        ("Auto", True, "Manual"),
        ("YOLO", True, "Manual"),
        ("FullYOLO", True, "Manual"),
        ("Manual", True, "Manual"),
        (None, True, "Manual"),
        ("Auto", False, "Auto"),
        ("YOLO", False, "YOLO"),
        ("Manual", False, "Manual"),
        (None, False, None),
    ],
)
def test_enforce_incognito_manual_only_touches_incognito(value, incognito, expected):
    assert enforce_incognito_manual(value, incognito=incognito) == expected


@pytest.mark.parametrize("mode", ["Auto", "YOLO"])
async def test_incognito_chat_runs_manual_even_with_a_saved_auto_row(sandbox_db, mode):
    """同一個 id 已存成 Auto／YOLO，再用無痕送出：不是「剛好讀不到」，仍是 Manual。"""
    sid = f"incognito-saved-{mode}"
    await _save_row(sid, mode)
    assert await _tool_layer_mode(incognito=True, mode=mode, session_id=sid) == "Manual"


@pytest.mark.parametrize("mode", ["Auto", "YOLO"])
async def test_same_saved_row_without_incognito_keeps_the_mode(sandbox_db, mode):
    sid = f"normal-saved-{mode}"
    await _save_row(sid, mode)
    assert await _tool_layer_mode(incognito=False, mode=mode, session_id=sid) == mode


@pytest.mark.parametrize("mode", ["Auto", "YOLO"])
@pytest.mark.parametrize("flag", ["heartbeat_run", "subagent_run"])
async def test_heartbeat_and_subagent_assembly_inherit_manual_when_incognito(flag, mode):
    """心跳保留 ctx 的模式、子代理組裝繼承父模式；帶著無痕標記時一律 Manual。"""
    assert await _tool_layer_mode(incognito=True, mode=mode, **{flag: True}) == "Manual"
    assert await _tool_layer_mode(incognito=False, mode=mode, **{flag: True}) == mode


@pytest.mark.parametrize("mode", ["Auto", "YOLO"])
@pytest.mark.parametrize("incognito", [True, False])
async def test_subagent_run_ctx_is_manual_when_parent_is_incognito(monkeypatch, mode, incognito):
    """子代理執行用的 ctx 不經 ToolLayer：父代理是無痕就一律 Manual，否則照父代理的模式。"""
    captured: dict[str, Any] = {}

    async def _factory(_model, _request, _identity, _rid, _t0, context):
        captured["ctx"] = context

        async def _gen():
            yield "data: [DONE]\n\n"

        return _gen()

    async def _fake_assemble(**_kw: Any) -> Any:
        return MagicMock(model="fake-brain")

    monkeypatch.setattr(subagent_tools, "get_stream_generator", _factory)
    monkeypatch.setattr(subagent_tools, "_assemble_subagent_request", _fake_assemble)
    await subagent_tools._run_subagent(
        goal="g",
        parent_identity=OWNER,
        parent_request_id="req-p",
        base_ctx=PipelineContext(auth_level=mode, incognito=incognito),
        model=None,
    )
    ctx = captured["ctx"]
    assert ctx.incognito is incognito
    assert ctx.auth_level == ("Manual" if incognito else mode)


# R-2c ① #8：request.incognito 或 ui_persistence.mode == "incognito" 任一成立就算無痕。
# schema 會擋下兩者不一致的 HTTP 請求；這裡用 model_construct 繞過驗證，證明執行層自己也看兩欄。
_INCOGNITO_ADMISSION = IncognitoStreamAdmission(
    mode="incognito", user_parent_id="u1", assistant_node_id="a1"
)


def _mismatched_request(sid: str, mode: str) -> OpenAIChatRequest:
    return OpenAIChatRequest.model_construct(
        model="claude-test-brain",
        messages=[{"role": "user", "content": "hi"}],
        session_id=sid,
        incognito=False,
        auth_level=mode,
        stream=True,
        ui_persistence=_INCOGNITO_ADMISSION,
    )


@pytest.mark.parametrize("mode", ["Auto", "YOLO"])
async def test_ui_persistence_incognito_alone_forces_manual(sandbox_db, mode):
    sid = f"incognito-admission-{mode}"
    await _save_row(sid, mode)
    ctx = PipelineContext(session_id=sid, agent_id=SOUL, identity=OWNER, incognito=False)
    await ToolLayer().process(_mismatched_request(sid, mode), ctx)
    assert ctx.auth_level == "Manual"


def test_proxy_context_marks_incognito_from_either_field():
    fast_request = MagicMock(headers={}, state=SimpleNamespace())
    ctx = PipelineContext()
    _apply_chat_request_context(ctx, _mismatched_request("s-x", "Auto"), fast_request, OWNER)
    assert ctx["incognito"] is True
    plain = OpenAIChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    ctx = PipelineContext()
    _apply_chat_request_context(ctx, plain, fast_request, OWNER)
    assert ctx["incognito"] is False
