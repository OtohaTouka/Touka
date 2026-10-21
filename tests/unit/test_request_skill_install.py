"""ADR-0148 Bug A — request_skill_install 自帶審批 gate（互動→自帶卡、headless→fail-closed）。

request_skill_install 有自帶的 EvolutionModal 審批（收 env 輸入），於 tool_executor 標記為
_SELF_APPROVING_TOOLS 跳過 broker（不雙重審批）。本檔驗工具本體的 self-gate：
- headless（_interactive=False）→ 立即 fail-closed，**不**呼叫 request_approval（不空等 180s）。
- 互動（_interactive=True）→ 呼叫其自帶 request_approval（EvolutionModal）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from src.models.identity import AgentIdentity
from src.tools.builtin.system import request_skill_install

_IDENTITY = AgentIdentity.for_role(agent_id="Installer", role="Owner", is_human=False)


@pytest.mark.asyncio
@patch("src.tools.builtin.system.request_approval")
async def test_headless_fail_closed_no_approval_call(mock_approval):
    """headless（_interactive 缺省/False）→ 回 fail-closed 錯誤，且從不呼叫 request_approval。"""
    mock_approval.return_value = (True, {})
    result = await request_skill_install(
        "some-skill", identity=_IDENTITY, _interactive=False
    )
    assert "interactive session" in result.lower()
    mock_approval.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tools.builtin.system.request_approval")
async def test_missing_interactive_flag_treated_as_headless(mock_approval):
    """未帶 _interactive kwarg（防禦性）→ 視同 headless fail-closed。"""
    mock_approval.return_value = (True, {})
    result = await request_skill_install("some-skill", identity=_IDENTITY)
    assert "interactive session" in result.lower()
    mock_approval.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tools.builtin.system.config_utils")
@patch("src.tools.builtin.system.request_approval")
async def test_interactive_calls_own_approval(mock_approval, mock_cfg):
    """互動（_interactive=True）→ 走自帶 request_approval（EvolutionModal）。

    使用者拒絕 → 回 Rejected，不寫入（此處只驗『有走自帶審批卡』這一環）。
    """
    mock_cfg.config_exists.return_value = True
    mock_cfg.read_config_json.return_value = {
        "skills": {
            "some-skill": {
                "name": "Some Skill",
                "description": "d",
                "mcp_config": {"command": "python", "args": ["-m", "x"]},
                "permissions": [],
                "env_requirements": [],
            }
        }
    }
    # request_approval 回拒絕（避免真的寫檔）
    mock_approval.return_value = (False, {})

    result = await request_skill_install(
        "some-skill", identity=_IDENTITY, _interactive=True, _emit_event=AsyncMock()
    )

    mock_approval.assert_awaited_once()
    assert "rejected" in result.lower() or "denied" in result.lower()
