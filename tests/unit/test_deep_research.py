"""Deep Research 模式測試（ADR-0087）。

驗證：
  - deep_research 旗標啟用時，SkillLayer 強制注入 deep-research-coordinator（提示詞層）。
  - 強制注入不受 can_use_skill RBAC 阻擋（系統能力 skill，由請求旗標把關；
    實際 research 工具仍受 get_allowed_tools RBAC 控制）。
  - 旗標關閉時不注入。
  - 請求 schema 與 PipelineContext 的 deep_research 欄位預設 False。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.main import app
from src.middleware.layers.skill import SkillLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

_IDENTITY = AgentIdentity.for_role(agent_id="TestAgent", role="Owner")
_COORD = "deep-research-coordinator"


def _ctx(deep_research: bool = False) -> PipelineContext:
    ctx = PipelineContext(identity=_IDENTITY)
    ctx["headers"] = {"x-touka-skills": ""}
    if deep_research:
        ctx["deep_research"] = True
    return ctx


def _skill(sid: str = "x") -> dict:
    return {"id": sid, "system_prompt": "body", "required_tools": [], "mcp_servers": []}


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_flag_injects_coordinator(mock_perm, mock_skill, mock_session):
    """deep_research=True → deep-research-coordinator 被注入。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.return_value = _skill(_COORD)
    mock_skill.apply_skills_to_request = MagicMock()

    ctx = _ctx(deep_research=True)
    await SkillLayer().process(MagicMock(), ctx)

    injected = mock_skill.apply_skills_to_request.call_args[0][1]
    assert _COORD in injected
    assert _COORD in ctx["active_skills"]


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_flag_injection_bypasses_skill_rbac(mock_perm, mock_skill, mock_session):
    """can_use_skill 全拒時，協調 Skill 仍因旗標被強制注入（提示詞層，不授予工具）。"""
    mock_perm.can_use_skill.return_value = False
    mock_skill.get_skill.return_value = _skill(_COORD)
    mock_skill.apply_skills_to_request = MagicMock()

    ctx = _ctx(deep_research=True)
    await SkillLayer().process(MagicMock(), ctx)

    injected = mock_skill.apply_skills_to_request.call_args[0][1]
    assert _COORD in injected


@pytest.mark.asyncio
@patch("src.middleware.layers.skill.session_service")
@patch("src.middleware.layers.skill.skill_service")
@patch("src.middleware.layers.skill.permission_service")
async def test_flag_off_no_coordinator(mock_perm, mock_skill, mock_session):
    """deep_research 未設 → 不注入協調 Skill。"""
    mock_perm.can_use_skill.return_value = True
    mock_skill.get_skill.return_value = _skill()
    mock_skill.apply_skills_to_request = MagicMock()

    ctx = _ctx(deep_research=False)
    await SkillLayer().process(MagicMock(), ctx)

    injected = mock_skill.apply_skills_to_request.call_args[0][1]
    assert _COORD not in injected


def test_request_schemas_default_false():
    """OpenAIChatRequest / AnthropicRequest 的 deep_research 預設 False、可設 True。"""
    assert OpenAIChatRequest(model="m", messages=[]).deep_research is False
    assert OpenAIChatRequest(model="m", messages=[], deep_research=True).deep_research is True
    assert AnthropicRequest(model="m", messages=[]).deep_research is False
    assert AnthropicRequest(model="m", messages=[], deep_research=True).deep_research is True


def test_context_deep_research_default_false():
    """PipelineContext.deep_research 預設 False；可經 ctx['key'] 寫入讀取。"""
    ctx = PipelineContext()
    assert ctx.deep_research is False
    ctx["deep_research"] = True
    assert ctx.get("deep_research") is True


# --------------------------------------------------------------------------- #
# 批⑤-E E47（ADR-0087）：request.deep_research → ctx 的 router 層 wiring
# 上列測試全部手設 ctx 直呼 SkillLayer＝對 proxy.py:64/:102 的接線免疫；
# 變異改 `=request.incognito` 或刪行全綠。
# --------------------------------------------------------------------------- #


def test_router_wires_deep_research_flag_into_ctx(monkeypatch):
    """POST /v1/messages {deep_research:true, incognito:false} → ctx["deep_research"] is True。

    變異 proxy.py:64 改 `=request.incognito` →（incognito 顯式 False）ctx 變 False → 紅；
    刪行 → ctx 無鍵 → 紅。
    """
    captured: dict = {}

    async def _spy(request, ctx, is_anthropic=True):
        captured["ctx"] = ctx
        return {"ok": True}

    monkeypatch.setattr("src.routers.proxy.process_proxy_request", _spy)
    app.dependency_overrides[get_verified_identity] = lambda: _IDENTITY
    app.state.context = {}
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/messages",
            json={
                "model": "ToukaSoul",
                "messages": [{"role": "user", "content": "hi"}],
                "deep_research": True,
                "incognito": False,
            },
        )
        assert resp.status_code == 200, resp.text
        ctx = captured["ctx"]
        assert ctx["deep_research"] is True  # 旗標真的從 request body 接進 ctx
        assert ctx["incognito"] is False  # 姊妹旗標未被交換（防 =request.incognito 誤植）
    finally:
        app.dependency_overrides.clear()
