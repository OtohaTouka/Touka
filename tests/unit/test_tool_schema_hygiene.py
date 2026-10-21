"""Tier 2 schema 衛生（原生工具介面重設計提案 §4 Tier 2）——TDD 紅燈.

範圍（非破壞、加法為主）：
  1. vault_tools 補 schema（vault enum＋動作 enum＋soul 唯讀說明）並進入廣告清單。
  2. identity/project_id 透明化——**描述層**明文「由系統自動注入」，刻意不進
     schema required（executor 注入永遠蓋過模型值；宣告 required 反而誘導模型編造）。
  3. 重疊工具描述補「何時用／何時不要用」邊界（project/vault/soul/research/bash）。
  4. RBAC 門檻寫進描述（create_* 已有「限 Owner/admin」→ 回歸鎖）。
  5. mission_tools 巢狀壓平：tasks 項改 task_id/dependency_task_ids（runtime 雙收
     正規化、向後相容舊 id/dependencies）；schema 只廣告 artifact_refs（runtime 仍
     收 artifact_ref）。
  6. 條件必填以 oneOf 提前到 schema（action 判別式→各動作 required 清單）。
  7. Gemini 出場轉換剝除 oneOf/anyOf/allOf/const（Gemini OpenAPI 子集不支援，
     不剝會 400 打爆 Gemini 工具調用）；Anthropic 出場只剝根層組合子（內層保留）。
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from memory_server.tools import memory_tools_schema
from src.adapters.openai_to_gemini import convert_openai_to_gemini
from src.adapters.tool_schema import openai_to_anthropic_tools
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.tools.builtin.schemas import (
    CREATE_MCP_SCHEMA,
    CREATE_SKILL_SCHEMA,
    RESEARCH_SCHEMA,
    SCAFFOLD_MCP_SCHEMA,
    UPDATE_MY_SOUL_SCHEMA,
    builtin_tools_schema,
)
from src.tools.mission_tools import MISSION_TOOLS_SCHEMA, mission_tools
from src.tools.project_tools import PROJECT_TOOLS_SCHEMA
from src.tools.terminal import EXECUTE_COMMAND_SCHEMA
from src.tools.vault_tools import VAULT_TOOLS_SCHEMA
from src.utils.permission_grants import NameGrant

# --------------------------------------------------------------------------- #
# 輔助
# --------------------------------------------------------------------------- #

_FILE_ACTION_REQUIRED = {
    "read": {"path"},
    "write": {"path", "content"},
    "append": {"path", "content"},
    "str_replace": {"path", "old_str", "new_str"},
    "list": set(),
    "find": {"pattern"},
    "grep": {"query"},
    "delete": {"path"},
}

_MISSION_ACTION_REQUIRED = {
    "initialize": {"goal"},
    "update_status": {"task_id", "status"},
    "await_dispatch_result": {"callback_token"},
    "get_board": set(),
    # ADR-0135：finalize 免必填（無 verdict＝驗收清單；帶 verdict＝記錄結論）
    "finalize": set(),
    # ADR-0137：request_review 需 task_id
    "request_review": {"task_id"},
}


def _fn(schema: dict[str, Any]) -> dict[str, Any]:
    return schema["function"]


def _params(schema: dict[str, Any]) -> dict[str, Any]:
    return _fn(schema)["parameters"]


def _branch_for(schema: dict[str, Any], action: str) -> dict[str, Any]:
    branches = _params(schema).get("oneOf") or []
    hits = [b for b in branches if b.get("properties", {}).get("action", {}).get("const") == action]
    assert len(hits) == 1, f"action '{action}' 應恰有一個 oneOf 分支（找到 {len(hits)}）"
    return hits[0]


def _assert_action_conditionals(schema: dict[str, Any], table: dict[str, set[str]]) -> None:
    actions = set(_params(schema)["properties"]["action"]["enum"])
    assert actions == set(table), "oneOf 表與 action enum 必須一一對應"
    consts = {
        b.get("properties", {}).get("action", {}).get("const") for b in _params(schema)["oneOf"]
    }
    assert consts == actions, "每個 action 都要有分支、且無多餘分支"
    for action, extras in table.items():
        branch = _branch_for(schema, action)
        assert set(branch.get("required", [])) == extras, (
            f"{_fn(schema)['name']}.{action} 的條件必填應為 {extras}"
        )


def _assert_no_combinators(node: Any) -> None:
    """遞迴斷言 Gemini schema 內不含 oneOf/anyOf/allOf/const。"""
    if isinstance(node, dict):
        for banned in ("oneOf", "anyOf", "allOf", "const"):
            assert banned not in node, f"Gemini schema 不得含 '{banned}'"
        for value in node.values():
            _assert_no_combinators(value)
    elif isinstance(node, list):
        for item in node:
            _assert_no_combinators(item)


def _perm_allow_all() -> MagicMock:
    perm = MagicMock()
    perm.get_allowed_tools.return_value = NameGrant.from_lists(["*"])
    perm.is_tool_allowed.side_effect = lambda name, grant: grant.allows(name)
    perm.is_mcp_server_allowed.return_value = True
    perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
    return perm


# --------------------------------------------------------------------------- #
# 1. vault_tools schema：存在、形狀、註冊
# --------------------------------------------------------------------------- #


def test_vault_schema_shape():
    fn = _fn(VAULT_TOOLS_SCHEMA)
    assert fn["name"] == "vault_tools"
    props = _params(VAULT_TOOLS_SCHEMA)["properties"]
    assert set(props["vault"]["enum"]) == {"personal", "legacy", "soul"}
    assert set(props["action"]["enum"]) == set(_FILE_ACTION_REQUIRED)
    assert _params(VAULT_TOOLS_SCHEMA)["required"] == ["vault", "action"]
    # soul 唯讀＋寫入替代路徑要寫在 schema 裡（模型才不會踩 Gate 2 拒絕）
    desc_all = fn["description"] + props["vault"]["description"]
    assert "唯讀" in desc_all
    assert "update_my_soul" in desc_all


def test_vault_schema_not_advertised_after_tier3():
    """Tier 3 雙軌：vault_tools 降為 shim（可執行、不廣告）——schema 常數保留作
    形狀文件與測試依據，但不再進 builtin_tools_schema（統一動詞取代之）。"""
    names = [s["function"]["name"] for s in builtin_tools_schema]
    assert "vault_tools" not in names
    assert "read" in names  # 統一動詞接手廣告


@pytest.mark.asyncio
async def test_file_verbs_advertised_for_owner():
    """Tier 3：Owner 的 Gate 1 廣告清單應含統一動詞、不含舊 god-tools。"""
    request = SimpleNamespace(
        tools=None,
        project_id=None,
        ide_context=None,
        system=None,
        messages=[{"role": "user", "content": "hi"}],
    )
    ctx = PipelineContext(identity=AgentIdentity.for_role(agent_id="DebugAgent", role="Owner"))
    ctx["agent_id"] = "DebugAgent"
    ctx["memory_tools_schema"] = memory_tools_schema
    with patch("src.middleware.layers.tool.permission_service", _perm_allow_all()):
        await ToolLayer().process(request, ctx)
    names = {t["function"]["name"] for t in request.tools if isinstance(t, dict)}
    assert {"read", "write", "edit"} <= names
    assert "vault_tools" not in names
    assert "project_tools" not in names


# --------------------------------------------------------------------------- #
# 2+3. 邊界描述與 identity/project_id 透明化
# --------------------------------------------------------------------------- #


def test_project_tools_boundary_and_injection_notes():
    desc = _fn(PROJECT_TOOLS_SCHEMA)["description"]
    assert "何時不要用" in desc
    assert "vault_tools" in desc  # 個人檔案的正確去處
    assert "project_id" in desc
    assert "自動注入" in desc  # 由系統注入、模型不需提供


def test_vault_tools_boundary_and_injection_notes():
    desc = _fn(VAULT_TOOLS_SCHEMA)["description"]
    assert "何時不要用" in desc
    assert "project_tools" in desc
    assert "自動注入" in desc  # identity 鎖定當前 agent


def test_update_my_soul_boundary_notes():
    desc = _fn(UPDATE_MY_SOUL_SCHEMA)["description"]
    assert "何時不要用" in desc
    assert "vault_tools" in desc  # 唯讀查看的替代路徑／一般檔案去處


def test_research_boundary_notes():
    desc = _fn(RESEARCH_SCHEMA)["description"]
    assert "何時不要用" in desc  # 本機專案/個人檔案內容不該用網搜


def test_execute_command_boundary_notes():
    """指令執行的邊界描述（Tier 3 後由 execute_command 承接、指路檔案動詞）。"""
    desc = _fn(EXECUTE_COMMAND_SCHEMA)["description"]
    assert "何時不要用" in desc
    assert "read" in desc  # 檔案讀寫優先走統一檔案動詞


def test_mission_tools_identity_note_and_intent():
    desc = _fn(MISSION_TOOLS_SCHEMA)["description"]
    assert "自動注入" in desc
    assert "何時使用" in desc  # 基準 mission-initialize 選錯 → 強化意圖訊號


# --------------------------------------------------------------------------- #
# 4. RBAC 門檻寫進描述（回歸鎖：已存在即鎖住不退化）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("schema", [CREATE_SKILL_SCHEMA, CREATE_MCP_SCHEMA, SCAFFOLD_MCP_SCHEMA])
def test_creation_tools_rbac_marker(schema: dict[str, Any]):
    assert "Owner/admin" in _fn(schema)["description"]


# --------------------------------------------------------------------------- #
# 5. mission_tools 壓平＋artifact_refs 合併
# --------------------------------------------------------------------------- #


def test_mission_tasks_flat_keys_in_schema():
    task_props = _params(MISSION_TOOLS_SCHEMA)["properties"]["tasks"]["items"]["properties"]
    assert "task_id" in task_props
    assert "dependency_task_ids" in task_props
    # 舊鍵不再廣告（runtime 仍雙收）
    assert "id" not in task_props
    assert "dependencies" not in task_props


def test_mission_schema_single_artifact_refs():
    props = _params(MISSION_TOOLS_SCHEMA)["properties"]
    assert "artifact_refs" in props
    assert "artifact_ref" not in props  # schema 只廣告複數；runtime 向後相容單數


def test_mission_status_notes_blocked_requires_blocker_detail():
    status_desc = _params(MISSION_TOOLS_SCHEMA)["properties"]["status"]["description"]
    assert "blocker_detail" in status_desc  # H1：BLOCKED 為 runtime 硬規則，寫進 schema


class _StubMissionManager:
    """捕捉 initialize_mission 收到的參數（正規化驗證用）。"""

    captured: dict[str, Any] = {}

    async def initialize_mission(
        self,
        *,
        identity: Any,
        goal: str,
        initial_tasks: list[dict[str, Any]] | None = None,
        target_agent_id: str | None = None,
    ) -> Any:
        _StubMissionManager.captured = {"goal": goal, "initial_tasks": initial_tasks}
        return SimpleNamespace(tasks=initial_tasks or [], metadata={})


@pytest.mark.asyncio
async def test_mission_initialize_normalizes_flat_and_legacy_task_keys():
    """新鍵（task_id/dependency_task_ids）與舊鍵（id/dependencies）都被正規化為引擎鍵。"""
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    tasks_in = [
        {"task_id": "t1", "description": "review", "dependency_task_ids": []},
        {"task_id": "t2", "description": "test", "dependency_task_ids": ["t1"]},
        {"id": "t3", "description": "legacy-style", "dependencies": ["t2"]},
    ]
    with patch("memory_server.services.mission_manager.MissionManager", _StubMissionManager):
        result = await mission_tools(
            action="initialize", identity=identity, goal="ship it", tasks=tasks_in
        )

    assert "Mission Initialized" in str(result)
    got = _StubMissionManager.captured["initial_tasks"]
    assert got == [
        {"id": "t1", "description": "review", "dependencies": []},
        {"id": "t2", "description": "test", "dependencies": ["t1"]},
        {"id": "t3", "description": "legacy-style", "dependencies": ["t2"]},
    ]


# --------------------------------------------------------------------------- #
# 6. 條件必填（oneOf、action 判別式）
# --------------------------------------------------------------------------- #


def test_project_tools_conditional_required():
    _assert_action_conditionals(PROJECT_TOOLS_SCHEMA, _FILE_ACTION_REQUIRED)


def test_vault_tools_conditional_required():
    _assert_action_conditionals(VAULT_TOOLS_SCHEMA, _FILE_ACTION_REQUIRED)


def test_mission_tools_conditional_required():
    _assert_action_conditionals(MISSION_TOOLS_SCHEMA, _MISSION_ACTION_REQUIRED)


# --------------------------------------------------------------------------- #
# 7. 供應商出場：Gemini 剝除組合子、Anthropic 只剝根層組合子
# --------------------------------------------------------------------------- #

_ONEOF_TOOL = {
    "type": "function",
    "function": {
        "name": "combo_tool",
        "description": "for conversion test",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["a", "b"]},
                "nested": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "anyOf": [{"required": ["x"]}],
                },
                "items_prop": {
                    "type": "array",
                    "items": {"type": "object", "allOf": [{"required": []}]},
                },
            },
            "required": ["action"],
            "oneOf": [
                {"properties": {"action": {"const": "a"}}, "required": ["nested"]},
                {"properties": {"action": {"const": "b"}}, "required": []},
            ],
        },
    },
}


def test_gemini_conversion_strips_schema_combinators():
    request = SimpleNamespace(
        messages=[{"role": "user", "content": "hi"}],
        tools=[_ONEOF_TOOL],
        safety_settings=None,
        safety_mode=None,
        temperature=1.0,
        top_p=None,
        top_k=None,
    )
    gemini_req = convert_openai_to_gemini(request)
    decls = gemini_req["tools"][0]["functionDeclarations"]
    assert decls[0]["name"] == "combo_tool"
    params = decls[0]["parameters"]
    _assert_no_combinators(params)
    # 既有行為不退化：型別仍轉大寫、properties 結構保留
    assert params["type"] == "OBJECT"
    assert params["properties"]["nested"]["type"] == "OBJECT"


def test_anthropic_conversion_strips_root_combinators_only():
    """Anthropic 不收根層 oneOf/anyOf/allOf（整包 400）；內層照留，輸入不被改。

    舊版本測試規定「轉給 Claude 要保留根層 oneOf」，把錯誤行為鎖住，已改寫。
    """
    original = copy.deepcopy(_ONEOF_TOOL)
    converted = openai_to_anthropic_tools([_ONEOF_TOOL])
    assert converted is not None
    schema = converted[0]["input_schema"]
    assert {"oneOf", "anyOf", "allOf"}.isdisjoint(schema)
    assert schema["type"] == "object"
    assert schema["required"] == ["action"]
    assert schema["properties"]["nested"]["anyOf"] == [{"required": ["x"]}]
    assert schema["properties"]["items_prop"]["items"]["allOf"] == [{"required": []}]
    assert original == _ONEOF_TOOL  # 模組層 schema 由 ToolLayer 以參考放進請求，不得就地改
