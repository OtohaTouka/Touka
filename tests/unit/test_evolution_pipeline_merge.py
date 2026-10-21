"""演化管線 4→2（提案 §7 最後一項；狀態機驗序）——TDD 紅燈.

現況：演化是 4 顆連續小工具（propose_new_capability → write_mcp_implementation →
verify_mcp_locally → request_mcp_creation），模型得記住順序、每步一個往返，且順序
錯誤只得到含糊錯誤。合併為 2 顆（廣告 29→27）：

  1. ``evolution_define``＝提案（委派 propose_new_capability，零重複）。
  2. ``evolution_implement_and_verify``＝實作＋沙盒驗證一次完成（寫碼→venv→pip→
     jail 測試），可選 ``submit_for_deploy``——**驗證通過才**觸發既有 HITL 部署
     （request_mcp_creation）。狀態機驗序：未 define（無 spec.json）→ 可讀
     validation_error；驗證未過 → 部署絕不觸發。
  3. 雙軌：舊 4 名保留為可執行 shim、不再廣告。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.services import tool_errors
from src.services.tool_executor import CRITICAL_TOOLS
from src.tools.builtin import evolution, tool_registry
from src.tools.builtin.schemas import builtin_tools_schema

_OLD_STEPS = {
    "propose_new_capability",
    "write_mcp_implementation",
    "verify_mcp_locally",
    "request_mcp_creation",
}

_WRITE_OK = (
    "Development Success:\nCode written to server.py.\n"
    "✅ Virtual environment created.\n\n"
    "Next, use 'verify_mcp_locally' to test your tool."
)
_VERIFY_GREEN = evolution.VerifyOutcome(
    True, 'Test Result for \'ping\':\n{"status": "success", "data": "pong"}\n')
_VERIFY_FAIL = evolution.VerifyOutcome(
    False, 'Test Result for \'ping\':\n{"status": "error", "message": "boom"}\n')


def _ads() -> set[str]:
    return {s["function"]["name"] for s in builtin_tools_schema}


# --------------------------------------------------------------------------- #
# 1. Schema／註冊／廣告翻轉
# --------------------------------------------------------------------------- #


def test_advertising_flip():
    ads = _ads()
    assert "evolution_define" in ads
    assert "evolution_implement_and_verify" in ads
    assert not (_OLD_STEPS & ads), "舊四步不應再廣告（雙軌 shim）"


def test_registry_and_critical():
    for name in ("evolution_define", "evolution_implement_and_verify", *_OLD_STEPS):
        assert name in tool_registry, f"'{name}' 應可執行"
    assert "evolution_define" in CRITICAL_TOOLS
    assert "evolution_implement_and_verify" in CRITICAL_TOOLS


def test_merged_schema_shape():
    schema = next(
        s
        for s in builtin_tools_schema
        if s["function"]["name"] == "evolution_implement_and_verify"
    )
    params = schema["function"]["parameters"]
    assert params["required"] == ["skill_id", "code", "test_tool_name", "test_arguments"]
    assert "submit_for_deploy" in params["properties"]
    # 狀態機語義寫進描述（模型需知道 define 先行、驗過才部署）
    assert "evolution_define" in schema["function"]["description"]


# --------------------------------------------------------------------------- #
# 2. evolution_define：委派提案＋指路新流程
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_define_delegates_to_propose():
    with patch.object(
        evolution, "propose_new_capability", new=AsyncMock(return_value="proposed-ok")
    ) as propose:
        result = await evolution.evolution_define(
            skill_id="s1", name="N", description="D", schema={"type": "object"}
        )
    assert result == "proposed-ok"
    assert propose.call_args.kwargs["skill_id"] == "s1"


@pytest.mark.asyncio
async def test_propose_guides_to_new_pipeline():
    """propose 的下一步指引應指向新正名（舊名已不廣告，指舊名＝幻覺誘因）。"""
    with patch.object(evolution, "EvolutionPathGuard") as guard:
        guard.mkdir_tool = MagicMock()
        guard.write_tool_file = MagicMock()
        result = await evolution.propose_new_capability(
            skill_id="s1", name="N", description="D", schema={}
        )
    assert "evolution_implement_and_verify" in result


# --------------------------------------------------------------------------- #
# 3. 狀態機驗序
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_implement_requires_define_first():
    with (
        patch.object(evolution.EvolutionPathGuard, "tool_file_exists", return_value=False),
        patch.object(evolution, "write_mcp_implementation", new=AsyncMock()) as write,
        patch.object(evolution, "_verify_mcp", new=AsyncMock()) as verify,
    ):
        result = await evolution.evolution_implement_and_verify(
            skill_id="ghost", code="print(1)", test_tool_name="t", test_arguments={}
        )
    write.assert_not_awaited()
    verify.assert_not_awaited()
    assert tool_errors.is_tool_error(result)
    payload = json.loads(result)
    assert payload["error_class"] == tool_errors.VALIDATION_ERROR
    assert "evolution_define" in payload.get("suggestion", "")


@pytest.mark.asyncio
async def test_implement_and_verify_happy_path_without_submit():
    with (
        patch.object(evolution.EvolutionPathGuard, "tool_file_exists", return_value=True),
        patch.object(
            evolution, "write_mcp_implementation", new=AsyncMock(return_value=_WRITE_OK)
        ),
        patch.object(
            evolution, "_verify_mcp", new=AsyncMock(return_value=_VERIFY_GREEN)
        ) as verify,
        patch.object(evolution, "request_mcp_creation", new=AsyncMock()) as deploy,
    ):
        result = await evolution.evolution_implement_and_verify(
            skill_id="s1",
            code="print(1)",
            test_tool_name="ping",
            test_arguments={"x": 1},
        )
    deploy.assert_not_awaited()  # 未要求部署
    assert verify.call_args.kwargs["tool_name"] == "ping"
    assert verify.call_args.kwargs["arguments"] == {"x": 1}
    assert "Code written to server.py" in result
    assert '"status": "success"' in result
    # 舊導覽尾句不得滲入合併輸出（指向未廣告的舊名＝幻覺誘因）
    assert "verify_mcp_locally" not in result
    # 通過且未部署 → 指引下一步
    assert "submit_for_deploy" in result


@pytest.mark.asyncio
async def test_env_failure_stops_before_verify():
    with (
        patch.object(evolution.EvolutionPathGuard, "tool_file_exists", return_value=True),
        patch.object(
            evolution,
            "write_mcp_implementation",
            new=AsyncMock(return_value="Error during environment setup: pip exploded"),
        ),
        patch.object(evolution, "_verify_mcp", new=AsyncMock()) as verify,
    ):
        result = await evolution.evolution_implement_and_verify(
            skill_id="s1", code="x", test_tool_name="t", test_arguments={}
        )
    verify.assert_not_awaited()
    assert "pip exploded" in result


@pytest.mark.asyncio
async def test_submit_blocked_when_verification_fails():
    with (
        patch.object(evolution.EvolutionPathGuard, "tool_file_exists", return_value=True),
        patch.object(
            evolution, "write_mcp_implementation", new=AsyncMock(return_value=_WRITE_OK)
        ),
        patch.object(
            evolution, "_verify_mcp", new=AsyncMock(return_value=_VERIFY_FAIL)
        ),
        patch.object(evolution, "request_mcp_creation", new=AsyncMock()) as deploy,
    ):
        result = await evolution.evolution_implement_and_verify(
            skill_id="s1",
            code="x",
            test_tool_name="ping",
            test_arguments={},
            submit_for_deploy=True,
        )
    deploy.assert_not_awaited()  # 驗證未過 → 絕不部署（狀態機）
    assert '"status": "error"' in result
    assert "not requested" in result or "skipped" in result or "未" in result


@pytest.mark.asyncio
async def test_submit_deploys_after_green_verification():
    identity = MagicMock()
    with (
        patch.object(evolution.EvolutionPathGuard, "tool_file_exists", return_value=True),
        patch.object(
            evolution, "write_mcp_implementation", new=AsyncMock(return_value=_WRITE_OK)
        ),
        patch.object(
            evolution, "_verify_mcp", new=AsyncMock(return_value=_VERIFY_GREEN)
        ),
        patch.object(
            evolution,
            "request_mcp_creation",
            new=AsyncMock(return_value="Evolution Success: deployed!"),
        ) as deploy,
    ):
        result = await evolution.evolution_implement_and_verify(
            skill_id="s1",
            code="x",
            test_tool_name="ping",
            test_arguments={},
            submit_for_deploy=True,
            identity=identity,
        )
    deploy.assert_awaited_once()
    assert deploy.call_args.kwargs["skill_id"] == "s1"
    assert deploy.call_args.kwargs["identity"] is identity  # HITL 身分傳遞
    assert "Evolution Success: deployed!" in result


# --------------------------------------------------------------------------- #
# 4. 模型多給重疊鍵不得炸 TypeError（防幻覺參數）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extra_hallucinated_kwargs_are_harmless():
    extra: dict[str, Any] = {"tool_name": "bogus", "arguments": {"y": 2}, "name": "x"}
    with (
        patch.object(evolution.EvolutionPathGuard, "tool_file_exists", return_value=True),
        patch.object(
            evolution, "write_mcp_implementation", new=AsyncMock(return_value=_WRITE_OK)
        ),
        patch.object(
            evolution, "_verify_mcp", new=AsyncMock(return_value=_VERIFY_GREEN)
        ) as verify,
    ):
        result = await evolution.evolution_implement_and_verify(
            skill_id="s1",
            code="x",
            test_tool_name="ping",
            test_arguments={"x": 1},
            **extra,
        )
    # schema 參數贏、幻覺鍵被剝除
    assert verify.call_args.kwargs["tool_name"] == "ping"
    assert verify.call_args.kwargs["arguments"] == {"x": 1}
    assert '"status": "success"' in result
