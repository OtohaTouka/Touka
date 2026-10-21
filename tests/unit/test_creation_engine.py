"""ADR-0069 CreationEngine 單元測試（脊椎 + Phase A SMOKE 關卡）。

沙箱由 ``tests/conftest.py`` 提供（``TOUKA_CONFIG_DIR`` → tmp）；所有產物寫入
沙箱，零接觸真實 agent 資料。涵蓋：未知 kind 拋例外、VALIDATE 關卡擋 COMMIT、
CREATE 不覆寫既有 id（保護既有 agent 資料）、skill/mcp 委派既有 SSOT 服務、
MCP 命令 shell 注入防護，以及 🆕 SMOKE 試跑關卡（mock 掉真實啟動）。

引擎 ``create`` 為 async（MCP 握手本質非同步），故測試皆 async。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from src.config import get_config_dir, settings
from src.core.exceptions import ToukaException
from src.protocols.mcp.environment import LAUNCH_ENV_ALLOWLIST
from src.services.creation import creation_engine
from src.services.creation import smoke as smoke_mod
from src.services.creation.kinds import launch_context
from src.services.creation.models import McpSpec, SkillSpec, SmokeReport
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service

_PATCH_MCP_SMOKE = "src.services.creation.mcp_kind.smoke_mcp_server"


def _server_config_path():
    return get_config_dir() / "server_config.json"


def _skill_md_path(skill_id: str):
    return get_config_dir() / "skills" / skill_id / "SKILL.md"


@pytest.fixture(autouse=True)
def _stub_eval_list_mcp_tools():
    """後置 EVAL（ADR-0069 Phase B）會枚舉 MCP 工具；測試 stub 掉真實握手保持 hermetic。"""
    with patch(
        "src.services.creation.mcp_kind.list_mcp_tools",
        new=AsyncMock(return_value=[]),
    ):
        yield


# --- 引擎關卡 ---


async def test_unknown_kind_raises():
    with pytest.raises(ToukaException):
        await creation_engine.create("nonexistent-kind", SkillSpec(id="x-skill"))


async def test_validate_gate_blocks_commit():
    res = await creation_engine.create(
        "skill", SkillSpec(id="Bad Id", description="d", system_prompt="b")
    )
    assert res.committed is False
    assert res.validation.ok is False
    assert not resource_manager.exists(_skill_md_path("Bad Id"))


# --- Skill kind ---


async def test_skill_commit_creates_skill():
    res = await creation_engine.create(
        "skill",
        SkillSpec(
            id="engine-demo-a", name="Demo A", description="d", system_prompt="hello"
        ),
    )
    assert res.committed is True
    assert res.commit is not None
    assert res.commit.id == "engine-demo-a"
    assert skill_service.get_skill("engine-demo-a") is not None


async def test_skill_rejects_existing_id():
    await creation_engine.create(
        "skill",
        SkillSpec(id="engine-demo-b", name="B", description="d", system_prompt="x"),
    )
    res = await creation_engine.create(
        "skill", SkillSpec(id="engine-demo-b", description="again", system_prompt="y")
    )
    assert res.committed is False
    assert any("存在" in e or "exist" in e.lower() for e in res.validation.errors)


async def test_skill_create_does_not_touch_existing():
    await creation_engine.create(
        "skill",
        SkillSpec(id="engine-keep", name="Keep", description="d", system_prompt="orig"),
    )
    before = resource_manager.read_file_text(_skill_md_path("engine-keep"))
    await creation_engine.create(
        "skill",
        SkillSpec(
            id="engine-fresh", name="Fresh", description="d2", system_prompt="body2"
        ),
    )
    after = resource_manager.read_file_text(_skill_md_path("engine-keep"))
    assert before == after


# --- Skill SMOKE（乾跑：round-trip + mcp_servers 解析）---


async def test_skill_smoke_roundtrip_ok():
    res = await creation_engine.create(
        "skill",
        SkillSpec(id="engine-smoke-ok", name="S", description="d", system_prompt="body"),
    )
    assert res.committed is True
    assert res.smoke is not None
    assert res.smoke.ok is True


async def test_skill_smoke_warns_unregistered_mcp_server():
    resource_manager.write_json(_server_config_path(), {"mcpServers": {}})
    res = await creation_engine.create(
        "skill",
        SkillSpec(
            id="engine-smoke-warn",
            name="S",
            description="d",
            system_prompt="body",
            mcp_servers=["ghost-server"],
        ),
    )
    # 未註冊的 mcp_server 只是警告，不擋 commit
    assert res.committed is True
    assert res.smoke is not None
    assert any("ghost-server" in w for w in res.smoke.warnings)


# --- MCP kind + SMOKE ---


async def test_mcp_validate_rejects_shell_injection():
    res = await creation_engine.create(
        "mcp", McpSpec(name="evil", command="sh;rm -rf /", args=[])
    )
    assert res.committed is False
    assert res.validation.ok is False


async def test_mcp_smoke_failure_blocks_commit():
    resource_manager.write_json(_server_config_path(), {"mcpServers": {}})
    with patch(
        _PATCH_MCP_SMOKE,
        new=AsyncMock(return_value=SmokeReport(ok=False, detail="boom")),
    ):
        res = await creation_engine.create(
            "mcp", McpSpec(name="failsrv", command="uvx", args=["x"]),
            context=await launch_context("creation"),
        )
    assert res.committed is False
    assert res.smoke is not None
    assert res.smoke.ok is False
    data = resource_manager.read_json(_server_config_path()) or {}
    assert "failsrv" not in data.get("mcpServers", {})


async def test_mcp_smoke_pass_then_commit():
    resource_manager.write_json(_server_config_path(), {"mcpServers": {}})
    with patch(
        _PATCH_MCP_SMOKE,
        new=AsyncMock(return_value=SmokeReport(ok=True, tool_count=2)),
    ):
        res = await creation_engine.create(
            "mcp", McpSpec(name="goodsrv", command="uvx", args=["mcp-server-fetch"]),
            context=await launch_context("creation"),
        )
    assert res.committed is True
    assert res.smoke is not None
    assert res.smoke.tool_count == 2
    data = resource_manager.read_json(_server_config_path())
    assert "goodsrv" in data["mcpServers"]


async def test_mcp_smoke_receives_spec_env():
    resource_manager.write_json(_server_config_path(), {"mcpServers": {}})
    captured: dict = {}

    async def _fake(command, args, env=None, timeout=30.0, *, name, context):
        captured["command"] = command
        captured["env"] = env
        return SmokeReport(ok=True, tool_count=1)

    with patch(_PATCH_MCP_SMOKE, new=_fake):
        await creation_engine.create(
            "mcp",
            McpSpec(name="envsrv", command="uvx", args=[], env={"MY_API": "abc"}),
            context=await launch_context("creation"),
        )
    # mcp_kind 把 spec.env 傳給 smoke_mcp_server（Touka 密鑰淨化在 smoke_mcp_server 內做）
    assert captured["command"] == "uvx"
    assert captured["env"] == {"MY_API": "abc"}


async def test_mcp_rejects_existing_name():
    resource_manager.write_json(
        _server_config_path(),
        {"mcpServers": {"fetch": {"command": "uvx", "args": [], "env": {}}}},
    )
    res = await creation_engine.create("mcp", McpSpec(name="fetch", command="uvx", args=[]))
    assert res.committed is False
    assert any("存在" in e or "exist" in e.lower() for e in res.validation.errors)


async def test_create_run_smoke_false_skips():
    resource_manager.write_json(_server_config_path(), {"mcpServers": {}})
    # 即使 smoke 會失敗，run_smoke=False 也直接 commit（離線/純設定情境）
    with patch(
        _PATCH_MCP_SMOKE,
        new=AsyncMock(return_value=SmokeReport(ok=False, detail="would fail")),
    ):
        res = await creation_engine.create(
            "mcp", McpSpec(name="skipsrv", command="uvx", args=[]), run_smoke=False
        )
    assert res.committed is True
    assert res.smoke is None


# --- smoke.py 白名單底合併（不碰 os.environ，patch settings.get_full_env；iso-c1 缺口 2）---


async def test_smoke_launch_env_merges_clean_base_and_spec_env():
    parent = {"LANG": "C.UTF-8", "SAFE": "1", "OPENAI_API_KEY": "leak"}
    with patch.object(type(settings), "get_full_env", lambda _self: dict(parent)):
        env = smoke_mod._build_launch_env({"DECLARED": "v"})
    assert "LANG" in LAUNCH_ENV_ALLOWLIST
    assert env == {"LANG": "C.UTF-8", "DECLARED": "v"}
