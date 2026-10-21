"""ADR-0069 Phase D — 創建 agent 工具（in-chat、Owner/admin 限定）。委派 services.creation.api。

身分由 ToolExecutor 注入（``kwargs['identity']``）；先 gate Owner/admin，再委派與路由
**同一個**門面（DRY）。回傳給 agent 的結果字串（失敗以 ❌ 開頭，供成功偵測）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.services.creation import api as creation_api
from src.services.creation.kinds import launch_context
from src.services.creation.models import (
    McpScaffoldSpec,
    McpSpec,
    ParamSpec,
    SkillSpec,
    ToolSpec,
)
from src.services.isolation.agent_command import take_grant

if TYPE_CHECKING:
    from src.services.creation.models import CreationResult

def _gate(identity: Any) -> str | None:
    """權限門檻：非 Owner/admin → 回拒絕字串；通過回 None。

    改讀雙欄位謂詞（ADR-0141 P3）：``is_owner_or_admin()``＝{Owner, admin}
    家族（含 P6-d 前的 admin 代打過渡條款），與拆分前 ``role in {Owner, admin}``
    行為等價（特徵化矩陣 test_tool_layer_creation_gate 對釘）。None 顯式先擋。
    """
    if identity is None or not identity.is_owner_or_admin():
        return "❌ 權限不足：創建工具僅限 Owner/admin。"
    return None


def _summarize(res: CreationResult) -> str:
    """CreationResult → 給 agent 的結果字串。"""
    if res.committed:
        eval_n = len(res.evaluation.tasks) if res.evaluation else 0
        cid = res.commit.id if res.commit else "?"
        return f"✅ 已創建 {cid}（SMOKE 通過；EVAL 基線 {eval_n} 題）。"
    if not res.validation.ok:
        return "❌ 驗證未通過：" + "；".join(res.validation.errors)
    if res.smoke and not res.smoke.ok:
        return f"❌ SMOKE 試跑失敗：{res.smoke.detail}"
    return "❌ 創建未完成（未知狀態）。"


async def create_skill(**kwargs: Any) -> str:
    """以創建引擎建立 skill（Owner/admin）。"""
    blocked = _gate(kwargs.get("identity"))
    if blocked:
        return blocked
    skill_id = kwargs.get("id")
    if not skill_id:
        return "❌ 缺少必要參數：id。"
    spec = SkillSpec(
        id=skill_id,
        name=kwargs.get("name", ""),
        description=kwargs.get("description", ""),
        category=kwargs.get("category", "General"),
        author=kwargs.get("author", "") or kwargs.get("agent_id", ""),
        system_prompt=kwargs.get("system_prompt", ""),
        required_tools=kwargs.get("required_tools") or [],
        mcp_servers=kwargs.get("mcp_servers") or [],
    )
    return _summarize(await creation_api.create_skill(spec))


async def create_mcp(**kwargs: Any) -> str:
    """以創建引擎註冊既有指令的 MCP server（Owner/admin）。"""
    blocked = _gate(kwargs.get("identity"))
    if blocked:
        return blocked
    name = kwargs.get("name")
    command = kwargs.get("command")
    if not name or not command:
        return "❌ 缺少必要參數：name 與 command。"
    spec = McpSpec(
        name=name,
        command=command,
        args=kwargs.get("args") or [],
        env=kwargs.get("env") or {},
        description=kwargs.get("description", ""),
    )
    context = await launch_context("creation", take_grant(
        kwargs["identity"], kwargs.get("session_id"), kwargs.get("_call_id"),
        tool="create_mcp", args=kwargs,
    ))
    return _summarize(await creation_api.create_mcp(spec, context=context))


async def scaffold_mcp(**kwargs: Any) -> str:
    """從零 scaffold 一個 MCP server stub 並走完整流水線（Owner/admin）。"""
    blocked = _gate(kwargs.get("identity"))
    if blocked:
        return blocked
    name = kwargs.get("name")
    raw_tools = kwargs.get("tools")
    if not name or not raw_tools:
        return "❌ 缺少必要參數：name 與 tools。"
    spec = McpScaffoldSpec(
        name=name,
        description=kwargs.get("description", ""),
        tools=[
            ToolSpec(
                name=t.get("name", ""),
                description=t.get("description", ""),
                params=[
                    ParamSpec(name=p.get("name", ""), type=p.get("type", "str"))
                    for p in (t.get("params") or [])
                ],
            )
            for t in raw_tools
        ],
    )
    context = await launch_context("scaffold", take_grant(
        kwargs["identity"], kwargs.get("session_id"), kwargs.get("_call_id"),
        tool="scaffold_mcp", args=kwargs,
    ))
    sres, cres = await creation_api.scaffold_and_create_mcp(spec, context=context)
    if not sres.ok:
        return "❌ Scaffold 失敗：" + "；".join(sres.errors)
    head = f"✅ 已生成 server stub：{sres.server_path}。"
    return head + (" " + _summarize(cres) if cres else "")
