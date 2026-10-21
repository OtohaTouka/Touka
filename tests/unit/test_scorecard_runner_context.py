"""TDD — 軌B Batch 2：計分卡 runner 與生產同源（context + 身分）。

證明：
1. ``_build_scorecard_context`` 把生產 base context（記憶工具 schema、protocol_manager、
   降級旗標）原樣帶進情境，並設好身分／agent_id／auth_level，且**刻意不設 resumable_stream**
   （自主執行不注入 ask_user，與心跳一致）。
2. 經 ToolLayer 後，計分卡情境的 ``request.tools`` 真的含 ``memory_tools``（canonical OpenAI
   形狀）——即「模型會被發到記憶工具」。純函式 + ToolLayer，零 API 花費。

這是本批次的回歸防線：擋「計分卡又把工具發成空（called: []）」重演。
"""
from __future__ import annotations

import pytest
from src.adapters.schemas import AnthropicRequest
from src.main import build_base_context
from src.middleware.layers.tool import ToolLayer
from src.models.identity import AgentIdentity
from src.services.scorecard.models import Scenario
from src.services.scorecard.runner import _build_scorecard_context, _missing_skill_mcp

_MEM_SCHEMA = [
    {
        "type": "function",
        "function": {"name": "memory_tools", "description": "查詢/儲存記憶", "parameters": {}},
    }
]
_SCENARIO = Scenario(
    id="tool_memory_recall", surface="react", prompt="查記憶", expect_tool="memory_tools"
)
# 計分卡實際解析出的身分（identity_map：DebugAgent→Debugger）。正常測試 sandbox 由
# conftest 播種生產 permissions，Debugger 依生產設定繼承 Owner；安全 fallback 刻意封閉。
_DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")


def test_build_scorecard_context_carries_production_base_ctx():
    """生產 base context 的關鍵鍵（記憶/protocol_manager/降級）原樣帶入；身分與旗標設妥。"""
    pm = object()
    base_ctx = {
        "memory_tools_schema": _MEM_SCHEMA,
        "protocol_manager": pm,
        "system_degraded": True,
        "degraded_reason": "x",
    }
    ctx = _build_scorecard_context(base_ctx, _DEBUGGER, _SCENARIO, t0=1.5)
    assert ctx.get("memory_tools_schema") == _MEM_SCHEMA
    assert ctx.get("protocol_manager") is pm
    assert ctx.get("system_degraded") is True
    assert ctx.identity is _DEBUGGER
    assert ctx.agent_id == "DebugAgent"
    assert ctx.is_toukasoul is True
    assert ctx.auth_level == "Manual"
    assert ctx.request_id == "scorecard-tool_memory_recall"
    # 刻意 divergence：自主執行不注入 ask_user → 不設 resumable_stream
    assert ctx.get("resumable_stream") is None


@pytest.mark.asyncio
async def test_scorecard_context_injects_memory_tools_through_toollayer():
    """端到端（零 API）：計分卡情境 + DebugAgent(Debugger) 經 ToolLayer → 注入 memory_tools。"""
    base_ctx = {"memory_tools_schema": _MEM_SCHEMA, "protocol_manager": None}
    ctx = _build_scorecard_context(base_ctx, _DEBUGGER, _SCENARIO, t0=0.0)
    req = AnthropicRequest(
        model="deepseek-v4-flash",
        max_tokens=512,
        messages=[{"role": "user", "content": "查記憶"}],
    )
    await ToolLayer().process(req, ctx)
    names = {t.get("function", {}).get("name") for t in (req.tools or [])}
    assert "memory_tools" in names, f"記憶工具未被注入（called 會是空的）：{names}"
    # canonical OpenAI 形狀（軌C）
    mem = next(t for t in req.tools if t.get("function", {}).get("name") == "memory_tools")
    assert mem["type"] == "function"


def test_build_base_context_provides_keys_scorecard_needs():
    """build_base_context（SSOT）須提供 ToolLayer/dispatch 需要的鍵——否則計分卡工具又歸空。

    lifespan 與計分卡 CLI 共用此函式（ADR-0101）；本測試擋「有人從 base context 移除
    memory_tools_schema 等關鍵鍵」的漂移回歸。
    """
    bc = build_base_context()
    for key in (
        "memory_tools_schema",
        "protocol_manager",
        "builtin_tools_schema",
        "memory_tool_registry",
        "system_degraded",
    ):
        assert key in bc, f"build_base_context 缺鍵 {key}（計分卡/聊天會少工具）"


# ---- Batch 3：乾淨模式（clean_slate → 無痕）----

def test_clean_slate_sets_incognito():
    """clean_slate=True → ctx.incognito（白紙基線：不讀記憶、零殘留）。"""
    ctx = _build_scorecard_context({}, _DEBUGGER, _SCENARIO, t0=0.0, clean_slate=True)
    assert ctx.get("incognito") is True
    # 預設（正常模式）不開無痕
    ctx_normal = _build_scorecard_context({}, _DEBUGGER, _SCENARIO, t0=0.0)
    assert ctx_normal.get("incognito") is False


class _FakeReg:
    def __init__(self, registered: bool) -> None:
        self._registered = registered

    def get_server(self, _name: str):
        return {"command": "uvx"} if self._registered else None


class _FakePM:
    def __init__(self, registered: bool) -> None:
        self.registry = _FakeReg(registered)


def test_missing_skill_mcp_detects_unregistered(monkeypatch):
    """技能所需 MCP 未註冊 → 回報缺失（docgen 未安裝時計分卡乾淨略過的依據）。"""
    monkeypatch.setattr(
        "src.services.scorecard.runner.skill_service.get_skill",
        lambda _sid: {"mcp_servers": ["mcp-docgen"]},
    )
    base = {"protocol_manager": _FakePM(registered=False)}
    assert _missing_skill_mcp(base, ("document-generator",)) == ["mcp-docgen"]


def test_missing_skill_mcp_empty_when_registered(monkeypatch):
    monkeypatch.setattr(
        "src.services.scorecard.runner.skill_service.get_skill",
        lambda _sid: {"mcp_servers": ["mcp-docgen"]},
    )
    base = {"protocol_manager": _FakePM(registered=True)}
    assert _missing_skill_mcp(base, ("document-generator",)) == []


def test_missing_skill_mcp_no_registry_does_not_skip():
    """無 registry 可查 → 不略過（照常跑），避免誤判。"""
    assert _missing_skill_mcp({}, ("document-generator",)) == []


def test_skills_preset_into_headers():
    """有 skills 的情境 → ctx headers 帶 x-touka-skills（供 SkillLayer 連其 MCP）。"""
    sc = Scenario(
        id="docgen_xlsx", surface="react", prompt="x", skills=("document-generator",)
    )
    ctx = _build_scorecard_context({}, _DEBUGGER, sc, t0=0.0)
    assert ctx.get("headers", {}).get("x-touka-skills") == "document-generator"
    # 無 skills 的情境不設此 header
    ctx2 = _build_scorecard_context({}, _DEBUGGER, _SCENARIO, t0=0.0)
    assert "x-touka-skills" not in (ctx2.get("headers") or {})


@pytest.mark.asyncio
async def test_clean_slate_run_injects_no_memory_tools():
    """乾淨模式經 ToolLayer → 記憶工具被 incognito 閘擋下（與正常模式注入形成 A/B）。"""
    base_ctx = {"memory_tools_schema": _MEM_SCHEMA, "protocol_manager": None}
    ctx = _build_scorecard_context(base_ctx, _DEBUGGER, _SCENARIO, t0=0.0, clean_slate=True)
    req = AnthropicRequest(
        model="deepseek-v4-flash",
        max_tokens=512,
        messages=[{"role": "user", "content": "查記憶"}],
    )
    await ToolLayer().process(req, ctx)
    names = {t.get("function", {}).get("name") for t in (req.tools or [])}
    assert "memory_tools" not in names
