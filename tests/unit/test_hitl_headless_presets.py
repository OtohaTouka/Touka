"""H1 — 無人 session 的權限「設定時決定」（hitl-unification 決策 3；owner 修改版）。

owner 決策：heartbeat／派工在**設定時**就事先決定 auth_level，整個 session 照該權限跑。

- **heartbeat**：`/heartbeat/awaken` body 新增明確三模式 ``auth_level``（優先，只收新名稱），
  未帶則沿 security_level 映射（safe→Auto、full→YOLO、看不懂→Manual；ADR-0246 U2c-5）。
  脈衝在 dispatch 前跑 ToolLayer（KF-4），ToolLayer 保留這個設定時決定的 ctx.auth_level、
  不採 session row 的模式，全部 pulse 沿用；不回寫 session row（home 模式共用人類家會話，
  不得踩掉 AuthSwitcher 的選擇）。
- **in-process 子代理**：兩階段一致——組裝（Gate-1 廣告面）原本硬編 "Manual"、執行
  （Gate-2）卻繼承父 ctx ＝ 廣告面與執行面用不同等級求值；修為兩階段都採父 session 的
  auth_level（人類派工當下的選擇＝事先決定；子代理只繼承、不可自升）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.routers.heartbeat import HeartbeatAwakenRequest, _resolve_request_auth_level
from src.services import proxy_service
from src.tools.subagent_tools import _assemble_subagent_request, _derive_context

# ── heartbeat：明確三模式優先、映射保留、非法值與舊名稱拒收 ──────────────────────


@pytest.mark.parametrize("level", ["Manual", "Auto", "YOLO"])
def test_awaken_explicit_auth_level_wins(level: str):
    req = HeartbeatAwakenRequest(token="t", auth_level=level, security_level="full")
    assert _resolve_request_auth_level(req) == level  # 明確值凌駕 security_level 映射


@pytest.mark.parametrize(
    ("security_level", "expected"),
    [("safe", "Auto"), ("full", "YOLO"), ("garbage", "Manual")],
)
def test_awaken_legacy_mapping_preserved(security_level: str, expected: str):
    """未帶 auth_level → security_level 映射；看不懂的值最嚴（U2c-5）。"""
    req = HeartbeatAwakenRequest(token="t", security_level=security_level)
    assert _resolve_request_auth_level(req) == expected


def test_awaken_rejects_invalid_auth_level():
    with pytest.raises(ValidationError):
        HeartbeatAwakenRequest(token="t", auth_level="GodMode")


@pytest.mark.parametrize("legacy", ["ReadOnly", "SafeAuto", "FullYOLO"])
def test_awaken_rejects_retired_auth_level_names(legacy: str):
    """API 輸入只收新名稱（ADR-0246）：舊名稱 422，不靜默對照。"""
    with pytest.raises(ValidationError):
        HeartbeatAwakenRequest(token="t", auth_level=legacy)


# ── 子代理：組裝與執行兩階段都繼承父 auth_level ─────────────────────────────────


@pytest.mark.asyncio
async def test_assemble_inherits_parent_auth_level(monkeypatch):
    """組裝階段（Gate-1 廣告面）不再硬編 Manual——採父 ctx 的 auth_level。"""
    captured: list[PipelineContext] = []

    async def _fake_execute(_request, ctx):
        captured.append(ctx)

    monkeypatch.setattr(proxy_service.pipeline, "execute", _fake_execute)

    parent_ctx = PipelineContext(auth_level="Auto", session_id="parent-sess")
    sub_identity = AgentIdentity.for_role(
        agent_id="__subagent_test1__", role="Subagent", is_human=False
    )
    await _assemble_subagent_request(
        goal="test goal", sub_identity=sub_identity, base_ctx=parent_ctx, model=None
    )

    assert captured, "pipeline.execute 未被呼叫"
    assert captured[0].auth_level == "Auto"
    assert captured[0].session_id is None  # 子代理不碰 session 狀態（既有語意保持）


@pytest.mark.asyncio
async def test_assemble_falls_back_manual_without_parent_level(monkeypatch):
    """父 ctx 無 auth_level → fail-safe Manual（不可憑空升級）。"""
    captured: list[PipelineContext] = []

    async def _fake_execute(_request, ctx):
        captured.append(ctx)

    monkeypatch.setattr(proxy_service.pipeline, "execute", _fake_execute)

    sub_identity = AgentIdentity.for_role(
        agent_id="__subagent_test2__", role="Subagent", is_human=False
    )
    await _assemble_subagent_request(
        goal="g", sub_identity=sub_identity, base_ctx=PipelineContext(), model=None
    )
    assert captured[0].auth_level == "Manual"


def test_derive_context_carries_auth_level_to_run_phase():
    """執行階段（Gate-2）：_derive_context 的 model_copy 保留父 auth_level（回歸鎖——
    子 ReAct loop 的 tool_executor 讀到的即父 session 等級）。"""
    parent = PipelineContext(auth_level="YOLO")
    inherited = _derive_context(parent).auth_level
    assert inherited == "YOLO"

    # dict 形父 ctx 同樣成立
    inherited_from_dict = _derive_context({"auth_level": "Auto"}).auth_level
    assert inherited_from_dict == "Auto"
