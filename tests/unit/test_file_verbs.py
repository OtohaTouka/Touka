"""Tier 3 統一裸動詞＋scope（原生工具介面重設計提案 §4/§6/§7）——TDD 紅燈.

已定案設計（2026-07-03）：檔案家族統一為 9 顆裸動詞工具（read/list/find/grep/
write/edit/insert/undo/delete），每顆參數形狀固定＋必填 `scope` enum；
**參數形狀由動詞決定、權限由 scope 決定**；嚴禁狀態機式範圍切換。
soul 讀統一（read-family scope 含 soul）、寫獨立（write-family scope 不含 soul、
專屬 `edit_soul`）；`sns_control` 假 enum → `send_message`。

雙軌（提案 §9）：新工具廣告、舊 god-tools（project_tools/vault_tools/
update_my_soul/sns_control）**保持可執行（shim）但不再廣告**——提案 §7 工具數學
（廣告約 27-28）即以此為前提；零邏輯重複＝新動詞為舊引擎的薄轉接層。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.resource_manager import resource_manager
from src.services.tool_executor import CRITICAL_TOOLS
from src.tools import file_verbs
from src.tools import vault_tools as vault_bridge
from src.tools.builtin import sns as sns_mod
from src.tools.builtin import system as system_mod
from src.tools.builtin import tool_registry
from src.tools.builtin.schemas import builtin_tools_schema
from src.utils.permission_grants import NameGrant

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

VERBS = ["read", "list", "find", "grep", "write", "edit", "insert", "undo", "delete"]
READ_FAMILY = {"read", "list", "find", "grep"}
WRITE_FAMILY = {"write", "edit", "insert", "undo", "delete"}
READ_SCOPES = {"project", "personal", "legacy", "soul"}
WRITE_SCOPES = {"project", "personal", "legacy"}

# 每顆動詞的固定形狀（top-level required；不再需要 oneOf——這正是 Tier 3 的重點）
EXPECTED_REQUIRED = {
    "read": ["scope", "path"],
    "list": ["scope"],
    "find": ["scope", "pattern"],
    "grep": ["scope", "query"],
    "write": ["scope", "path", "content"],
    "edit": ["scope", "path", "old_str", "new_str"],
    "insert": ["scope", "path", "line", "text"],
    "undo": ["scope", "path"],
    "delete": ["scope", "path"],
}

OLD_GOD_TOOLS = {"project_tools", "vault_tools", "update_my_soul", "sns_control"}


def _schema_by_name(name: str) -> dict[str, Any]:
    for s in builtin_tools_schema:
        if s["function"]["name"] == name:
            return s
    msg = f"schema '{name}' not in builtin_tools_schema"
    raise AssertionError(msg)


def _advertised_names() -> set[str]:
    return {s["function"]["name"] for s in builtin_tools_schema}


# --------------------------------------------------------------------------- #
# 1. Schema 形狀（固定 required、scope enum 讀寫家族分明）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("verb", VERBS)
def test_verb_schema_shape(verb: str):
    schema = _schema_by_name(verb)
    params = schema["function"]["parameters"]
    assert params["required"] == EXPECTED_REQUIRED[verb]
    scope_enum = set(params["properties"]["scope"]["enum"])
    if verb in READ_FAMILY:
        assert scope_enum == READ_SCOPES  # soul 讀統一
    else:
        assert scope_enum == WRITE_SCOPES  # 「非法狀態不可表示」：寫家族無 soul 選項
    # 固定形狀＝不需要 oneOf 條件必填
    assert "oneOf" not in params


def test_grep_has_no_path_param():
    """引擎 core_grep 不支援 path 過濾——schema 不廣告做不到的參數。"""
    props = _schema_by_name("grep")["function"]["parameters"]["properties"]
    assert "path" not in props


def test_write_mode_enum():
    props = _schema_by_name("write")["function"]["parameters"]["properties"]
    assert set(props["mode"]["enum"]) == {"overwrite", "append", "create_only"}


def test_edit_soul_and_send_message_schemas():
    edit_soul = _schema_by_name("edit_soul")
    actions = set(
        edit_soul["function"]["parameters"]["properties"]["action"]["enum"]
    )
    # 只承載寫入儀式：讀取走統一 read(scope=soul)
    assert actions == {"write", "str_replace", "insert", "undo_edit", "delete"}

    send_message = _schema_by_name("send_message")
    sm_params = send_message["function"]["parameters"]
    assert "action" not in sm_params["properties"]  # 假 enum 已移除
    assert sm_params["required"] == ["platform", "channel_id", "content"]


# --------------------------------------------------------------------------- #
# 2. 註冊與廣告翻轉（雙軌：舊工具可執行、不廣告）
# --------------------------------------------------------------------------- #


def test_new_tools_registered_and_old_shims_kept():
    for name in [*VERBS, "edit_soul", "send_message"]:
        assert name in tool_registry, f"'{name}' 應註冊為可執行工具"
    for old in OLD_GOD_TOOLS:
        assert old in tool_registry, f"舊工具 '{old}' 應保留為 shim（向後相容）"


def test_old_god_tools_not_advertised():
    ads = _advertised_names()
    assert not (OLD_GOD_TOOLS & ads), "舊 god-tools 不應再進廣告清單（雙軌）"
    for name in [*VERBS, "edit_soul", "send_message"]:
        assert name in ads


@pytest.mark.asyncio
async def test_toollayer_advertises_verbs_for_owner():
    perm = MagicMock()
    perm.get_allowed_tools.return_value = NameGrant.from_lists(["*"])
    perm.is_tool_allowed.side_effect = lambda name, grant: grant.allows(name)
    perm.is_mcp_server_allowed.return_value = True
    perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)

    request = SimpleNamespace(
        tools=None, project_id=None, ide_context=None, system=None,
        messages=[{"role": "user", "content": "hi"}],
    )
    ctx = PipelineContext(identity=AgentIdentity.for_role(agent_id="DebugAgent", role="Owner"))
    ctx["agent_id"] = "DebugAgent"
    with patch("src.middleware.layers.tool.permission_service", perm):
        await ToolLayer().process(request, ctx)
    names = {t["function"]["name"] for t in request.tools if isinstance(t, dict)}
    assert set(VERBS) <= names
    assert "edit_soul" in names
    assert not (OLD_GOD_TOOLS & names)


def test_critical_tools_covers_new_names():
    for name in [*VERBS, "edit_soul", "send_message"]:
        assert name in CRITICAL_TOOLS


# --------------------------------------------------------------------------- #
# 3. 委派映射（scope→引擎；參數轉接）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_read_delegates_to_project_engine_with_view_range():
    with patch.object(
        file_verbs, "project_tools", new=AsyncMock(return_value="ok")
    ) as proj:
        result = await file_verbs.verb_read(
            scope="project", path="src/a.py", view_range=[3, 5], project_id="p1"
        )
    assert result == "ok"
    kwargs = proj.call_args.kwargs
    assert kwargs["action"] == "read"
    assert kwargs["path"] == "src/a.py"
    # view_range [3,5]（1-based 含端）→ 引擎 offset=2, limit=3
    assert kwargs["offset"] == 2
    assert kwargs["limit"] == 3
    assert kwargs["project_id"] == "p1"


@pytest.mark.asyncio
async def test_read_delegates_to_vault_engine():
    with patch.object(
        file_verbs, "vault_tools", new=AsyncMock(return_value="soul-content")
    ) as vault:
        result = await file_verbs.verb_read(scope="soul", path="soul.md")
    assert result == "soul-content"
    kwargs = vault.call_args.kwargs
    assert kwargs["vault"] == "soul"
    assert kwargs["action"] == "read"


@pytest.mark.asyncio
async def test_list_maps_depth_to_max_depth():
    with patch.object(
        file_verbs, "vault_tools", new=AsyncMock(return_value="tree")
    ) as vault:
        await file_verbs.verb_list(scope="personal", depth=4)
    assert vault.call_args.kwargs["max_depth"] == 4


@pytest.mark.asyncio
async def test_write_mode_mapping():
    with patch.object(
        file_verbs, "project_tools", new=AsyncMock(return_value="Success: Wrote to x")
    ) as proj:
        await file_verbs.verb_write(
            scope="project", path="x.txt", content="hi", mode="overwrite"
        )
        assert proj.call_args.kwargs["action"] == "write"

        await file_verbs.verb_write(
            scope="project", path="x.txt", content="hi", mode="append"
        )
        assert proj.call_args.kwargs["action"] == "append"


@pytest.mark.asyncio
async def test_write_create_only_refuses_existing():
    # 探測讀成功（引擎回內容標頭）→ 檔案已存在 → 結構化 validation_error、不寫入
    probe = "--- Read x.txt (Lines 1 to 1 of 1) ---\nhello"
    with patch.object(
        file_verbs, "project_tools", new=AsyncMock(return_value=probe)
    ) as proj:
        result = await file_verbs.verb_write(
            scope="project", path="x.txt", content="hi", mode="create_only"
        )
    assert tool_errors.is_tool_error(result)
    assert json.loads(result)["error_class"] == tool_errors.VALIDATION_ERROR
    # 只有探測讀、沒有寫入
    assert all(c.kwargs.get("action") == "read" for c in proj.call_args_list)


@pytest.mark.asyncio
async def test_write_create_only_writes_when_missing():
    responses = ["Error: File not found or is a directory: x.txt", "Success: Wrote to x.txt"]
    mock = AsyncMock(side_effect=responses)
    with patch.object(file_verbs, "project_tools", new=mock):
        result = await file_verbs.verb_write(
            scope="project", path="x.txt", content="hi", mode="create_only"
        )
    assert result == "Success: Wrote to x.txt"
    assert mock.call_args_list[1].kwargs["action"] == "write"


@pytest.mark.asyncio
async def test_edit_then_undo_restores_previous_content():
    file_verbs.reset_undo_for_test()
    store = {"note.txt": "old content"}

    async def fake_vault(**kwargs: Any) -> str:
        action = kwargs["action"]
        path = kwargs["path"]
        if action == "read":
            body = store.get(path)
            if body is None:
                return f"Error: File not found or is a directory: {path}"
            return f"--- Read {path} (Lines 1 to 1 of 1) ---\n{body}"
        if action == "str_replace":
            store[path] = store[path].replace(kwargs["old_str"], kwargs["new_str"])
            return f"Success: Modified {path}"
        if action == "write":
            store[path] = kwargs["content"]
            return f"Success: Wrote to {path}"
        return f"Error: Unknown action '{action}'"

    with patch.object(file_verbs, "vault_tools", new=AsyncMock(side_effect=fake_vault)):
        edited = await file_verbs.verb_edit(
            scope="personal", path="note.txt", old_str="old", new_str="new",
            agent_id="DebugAgent",
        )
        assert tool_errors.is_tool_success(edited)
        assert store["note.txt"] == "new content"
        # P2a-2：回傳變更後片段（帶行號），模型免重讀即可驗證
        assert "new content" in json.loads(edited)["preview"]

        undone = await file_verbs.verb_undo(
            scope="personal", path="note.txt", agent_id="DebugAgent"
        )
        assert "Success" in undone  # undo 不 echo 內容、維持原字串
        assert store["note.txt"] == "old content"
    file_verbs.reset_undo_for_test()


@pytest.mark.asyncio
async def test_undo_without_history_is_legible():
    file_verbs.reset_undo_for_test()
    result = await file_verbs.verb_undo(
        scope="personal", path="never-edited.txt", agent_id="DebugAgent"
    )
    assert tool_errors.is_tool_error(result)
    assert json.loads(result)["error_class"] == tool_errors.RESOURCE_NOT_FOUND


@pytest.mark.asyncio
async def test_insert_places_text_at_line():
    file_verbs.reset_undo_for_test()
    store = {"a.txt": "line1\nline2\n"}

    async def fake_vault(**kwargs: Any) -> str:
        action = kwargs["action"]
        path = kwargs["path"]
        if action == "read":
            return f"--- Read {path} (Lines 1 to 2 of 2) ---\n{store[path]}"
        if action == "write":
            store[path] = kwargs["content"]
            return f"Success: Wrote to {path}"
        return "Error: unexpected"

    with patch.object(file_verbs, "vault_tools", new=AsyncMock(side_effect=fake_vault)):
        result = await file_verbs.verb_insert(
            scope="personal", path="a.txt", line=2, text="inserted",
            agent_id="DebugAgent",
        )
    assert tool_errors.is_tool_success(result)
    assert store["a.txt"] == "line1\ninserted\nline2\n"
    # P2a-2：回傳含插入行的變更後片段
    assert "inserted" in json.loads(result)["preview"]
    file_verbs.reset_undo_for_test()


def test_applied_hunk_numbers_and_marks_change_line():
    """P2a-2：_applied_hunk 回傳帶行號的變更周邊片段、標記變更行。"""
    content = "a\nb\nc\nd\ne\nf\ng"
    hunk = file_verbs._applied_hunk(content, line_1based=4, radius=1)
    lines = hunk.splitlines()
    # radius=1 → 第 3、4、5 行
    assert len(lines) == 3
    assert "3| c" in lines[0]
    assert lines[1].startswith(">")  # 變更行標記
    assert "4| d" in lines[1]
    assert "5| e" in lines[2]


def test_applied_hunk_clamps_bounds_and_handles_empty():
    assert file_verbs._applied_hunk("", 1) == ""
    # 超界行號夾到檔尾、不爆
    single = file_verbs._applied_hunk("only", line_1based=99, radius=3)
    assert "1| only" in single


# --------------------------------------------------------------------------- #
# 4. soul 防線與 scope 驗證
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_write_family_rejects_soul_scope():
    with patch.object(file_verbs, "vault_tools", new=AsyncMock()) as vault:
        result = await file_verbs.verb_write(
            scope="soul", path="soul.md", content="hack"
        )
    vault.assert_not_called()  # 引擎連碰都不碰
    assert tool_errors.is_tool_error(result)
    payload = json.loads(result)
    assert payload["error_class"] == tool_errors.PERMISSION_DENIED
    assert "edit_soul" in payload.get("suggestion", "")


@pytest.mark.asyncio
async def test_unknown_scope_is_validation_error():
    result = await file_verbs.verb_read(scope="banana", path="x")
    assert tool_errors.is_tool_error(result)
    assert json.loads(result)["error_class"] == tool_errors.VALIDATION_ERROR


# --------------------------------------------------------------------------- #
# 5. edit_soul（寫入儀式）與 send_message
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_edit_soul_delegates_write_to_update_my_soul():
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    with patch.object(
        system_mod, "update_my_soul", new=AsyncMock(return_value="Success: soul updated")
    ) as soul:
        result = await system_mod.edit_soul(
            action="write", type="soul", content="new me", identity=identity
        )
    assert result == "Success: soul updated"
    assert soul.call_args.kwargs["action"] == "write"


@pytest.mark.asyncio
async def test_edit_soul_rejects_read_actions():
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")
    result = await system_mod.edit_soul(action="read", type="soul", identity=identity)
    assert tool_errors.is_tool_error(result)
    payload = json.loads(result)
    assert payload["error_class"] == tool_errors.VALIDATION_ERROR
    assert "read" in payload.get("suggestion", "")  # 指路統一 read(scope=soul)


@pytest.mark.asyncio
async def test_send_message_and_sns_shim():
    with patch.object(
        sns_mod.bot_service, "send_external_message", new=AsyncMock(return_value=True)
    ) as sender:
        ok = await sns_mod.send_message(
            platform="discord", channel_id="c1", content="hi"
        )
        assert "Success" in ok or "success" in ok.lower()

        # 舊名 shim：sns_control(action=send_message) 仍可用
        legacy = await sns_mod.sns_control(
            action="send_message", platform="discord", channel_id="c1", content="hi"
        )
        assert "Success" in legacy or "success" in legacy.lower()
    assert sender.await_count == 2


# --------------------------------------------------------------------------- #
# 6. permissions.json：Subagent（改名前的 Guest）拿到動詞、edit_soul 被拒
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cfg",
    [
        # live 權限設定檔已改成新版面（人類 10/12 同意，ADR-0245）
        "ToukaProxy/config/permissions.json",
        "ToukaProxy/config/permissions.json.example",
    ],
)
def test_subagent_permissions_updated(cfg: str):
    data = json.loads((_PROJECT_ROOT / cfg).read_text(encoding="utf-8"))
    sub = data["roles"]["Subagent"]
    assert set(VERBS) <= set(sub["allowed_tools"]), f"{cfg}: Subagent 應獲得 9 動詞"
    assert "edit_soul" in sub["denied_tools"], (
        f"{cfg}: Subagent 應拒絕 edit_soul（鏡像 update_my_soul）"
    )


# --------------------------------------------------------------------------- #
# 批⑤-E E20（ADR-0022）：vault_tools 橋接 DI——resource_proxy 注入＋agent_id 錨定
# 唯一注入點 vault_tools.py:61、dispatcher 無 fallback；既有測試 mock 掉 impl 只斷言
# 上層行為，從未捕捉「轉發 payload」本身。
# --------------------------------------------------------------------------- #


async def test_vault_bridge_injects_resource_proxy_and_anchors_agent_id(monkeypatch):
    """橋接轉發 payload 必含 resource_proxy（DI 唯一來源）且 agent_id 錨定驗證身分。

    變異刪 :61 `payload["resource_proxy"] = resource_manager` → KeyError → 紅。
    """
    captured: dict[str, Any] = {}

    async def _impl(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "Success"

    monkeypatch.setattr(vault_bridge, "memory_vault_impl", _impl)
    ident = AgentIdentity.for_role(agent_id="RealVaultAgent", role="Base")

    res = await vault_bridge.vault_tools(
        vault="personal", action="list", identity=ident, agent_id="ForgedAgent"
    )

    assert res == "Success"
    assert captured["resource_proxy"] is resource_manager  # DI 注入（記憶引擎唯一路徑源）
    assert captured["agent_id"] == "RealVaultAgent"  # 呼叫者傳入的偽 agent_id 被驗證身分蓋掉
    assert captured["vault"] == "personal" and captured["action"] == "list"
