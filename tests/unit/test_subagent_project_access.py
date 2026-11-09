"""子代理看得到對話綁定的專案；靈魂可以限制成只能看（ADR-0309）。

整條路都是真的：資料庫裡的對話與專案、子代理的組裝與執行、工具清單、權限判定、
檔案工具。只有模型的回應是假的，所以組裝和執行兩個 ctx 只寫了其中一個也抓得到。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.config import get_app_data_dir
from src.middleware.layers.context import ContextLayer
from src.middleware.layers.tool import ToolLayer
from src.middleware.pipeline import ProxyPipeline
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.safety import PolicyContext
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.tools import subagent_tools
from src.tools.builtin import tool_registry
from src.utils import async_db

from tests.helpers.claude_stream import delegate_stream_to_create
from tests.support.session_scope import A, scope_db, seed  # noqa: F401

MODEL = "claude-3-5-sonnet"
TEXT_IN_A = "only-in-project-a"
OTHER = "only-in-project-b"
SUB = AgentIdentity.for_role("__subagent_probe__", "Subagent")
READS = ("read", "list", "find", "grep")
CHANGES = ("write", "edit", "insert", "undo", "delete", "ide_open_diff")


def _reply(*calls: tuple[str, dict], text: str | None = None) -> MagicMock:
    """一個假的模型回應：要呼叫的工具，或最後的文字。"""
    blocks: list[MagicMock] = []
    dumped: list[dict[str, Any]] = []
    for index, (name, args) in enumerate(calls):
        block = MagicMock()
        block.type = "tool_use"
        block.id = f"call-{name}-{index}"
        block.name = name
        block.input = args
        blocks.append(block)
        dumped.append({"type": "tool_use", "id": block.id, "name": name, "input": args})
    if text is not None:
        block = MagicMock()
        block.type = "text"
        block.text = text
        blocks.append(block)
        dumped.append({"type": "text", "text": text})
    reply = MagicMock()
    reply.content = blocks
    reply.stop_reason = "tool_use" if calls else "end_turn"
    reply.usage = MagicMock()
    reply.usage.model_dump.return_value = {"input_tokens": 1, "output_tokens": 1}
    reply.model_dump.return_value = {
        "id": "m",
        "model": MODEL,
        "role": "assistant",
        "content": dumped,
        "stop_reason": reply.stop_reason,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return reply


def _client(*replies: Any) -> MagicMock:
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=[*replies, _reply(text="完成")])
    return client


def _advertised(client: MagicMock) -> set[str]:
    return {tool["name"] for tool in client.messages.create.call_args_list[0].kwargs["tools"]}


def _results(client: MagicMock) -> str:
    """模型這一路收到的所有工具結果。"""
    found: list[Any] = []
    for message in client.messages.create.call_args_list[-1].kwargs["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            found += [block for block in content if block.get("type") == "tool_result"]
    return json.dumps(found, ensure_ascii=False)


def _parent(client: MagicMock, session_id: str | None, mode: str, **extra: Any) -> PipelineContext:
    return PipelineContext(
        anthropic_client=delegate_stream_to_create(client),
        protocol_manager=MagicMock(get_all_tools=AsyncMock(return_value=[])),
        tool_registry=tool_registry,
        memory_tool_registry={},
        plugin_registry={},
        request_id="parent",
        t0=0.0,
        agent_id=A.agent_id,
        identity=A,
        session_id=session_id,
        auth_level=mode,
        **extra,
    )


async def _run(
    client: MagicMock,
    session_id: str | None,
    mode: str = "Manual",
    *,
    read_only: bool = False,
    **extra: Any,
) -> PipelineContext:
    parent = _parent(client, session_id, mode, **extra)
    await subagent_tools._run_subagent(
        goal="照指示做",
        parent_identity=A,
        parent_request_id="parent",
        base_ctx=parent,
        model=MODEL,
        read_only=read_only,
    )
    return parent


async def _add_project(project_id: str, root: Path) -> None:
    async with async_db.get_async_db() as db:
        await db.execute(
            "INSERT INTO projects (id, name, root_path, owner_user_id, updatedAt) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, project_id, str(root), "scope-human", 1000),
        )
        await db.commit()


@pytest.fixture
def world(scope_db, tmp_path, monkeypatch):  # noqa: F811
    """工作區在 app 資料根之外（裡面的位置 Touka 會保護），組裝走真的工具層。"""
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    monkeypatch.setattr(resource_manager, "_workspace_root_cached", workspace)
    monkeypatch.setattr(
        "src.services.proxy_service.pipeline", ProxyPipeline([ContextLayer(), ToolLayer()])
    )
    return workspace


async def _bound(workspace: Path, name: str = "a", text: str = TEXT_IN_A) -> tuple[str, Path]:
    """建一個專案和綁定它的對話；回傳對話編號與專案資料夾。"""
    root = workspace / name
    root.mkdir()
    (root / "note.txt").write_text(text, encoding="utf-8")
    await _add_project(f"proj-{name}", root)
    session_id = f"chat-{name}"
    await seed(session_id, project_id=f"proj-{name}")
    return session_id, root


READ_NOTE = ("read", {"scope": "project", "path": "note.txt"})


# ── 看得到哪個專案 ───────────────────────────────────────────────


async def test_a_subagent_reads_the_project_bound_to_the_conversation(world):
    session_id, _ = await _bound(world)
    client = _client(_reply(READ_NOTE))
    await _run(client, session_id)
    assert TEXT_IN_A in _results(client)


async def test_without_a_bound_project_it_cannot_read_even_if_the_model_names_one(world):
    await _bound(world)
    await seed("chat-free")
    client = _client(
        _reply(("read", {"scope": "project", "path": "note.txt", "project_id": "proj-a"}))
    )
    await _run(client, "chat-free")
    assert TEXT_IN_A not in _results(client)


async def test_the_model_cannot_switch_to_another_project(world):
    session_id, _ = await _bound(world)
    await _bound(world, "b", OTHER)
    client = _client(
        _reply(("read", {"scope": "project", "path": "note.txt", "project_id": "proj-b"}))
    )
    await _run(client, session_id)
    assert OTHER not in _results(client)


async def test_rebinding_during_the_run_does_not_move_the_subagent(world):
    session_id, _ = await _bound(world)
    other_root = world / "b"
    other_root.mkdir()
    (other_root / "note.txt").write_text(OTHER, encoding="utf-8")
    await _add_project("proj-b", other_root)
    client = MagicMock()
    replies = iter([_reply(READ_NOTE), _reply(text="完成")])

    async def create(**_kwargs: Any) -> MagicMock:
        await seed(session_id, project_id="proj-b")
        return next(replies)

    client.messages.create = AsyncMock(side_effect=create)
    await _run(client, session_id)
    assert TEXT_IN_A in _results(client)
    assert OTHER not in _results(client)


async def test_a_project_outside_the_workspace_is_refused(world, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text(TEXT_IN_A, encoding="utf-8")
    await _add_project("proj-out", outside)
    await seed("chat-out", project_id="proj-out")
    client = _client(_reply(READ_NOTE))
    await _run(client, "chat-out")
    assert TEXT_IN_A not in _results(client)


async def test_a_project_inside_a_protected_location_is_refused(world, monkeypatch):
    app_root = Path(get_app_data_dir()).resolve()
    monkeypatch.setattr(resource_manager, "_workspace_root_cached", app_root)
    root = app_root / "protected-project"
    root.mkdir(exist_ok=True)
    (root / "note.txt").write_text(TEXT_IN_A, encoding="utf-8")
    await _add_project("proj-p", root)
    await seed("chat-p", project_id="proj-p")
    client = _client(_reply(READ_NOTE))
    await _run(client, "chat-p")
    assert TEXT_IN_A not in _results(client)


async def test_a_project_reached_through_a_link_is_refused(world):
    real = world / "real"
    real.mkdir()
    (real / "note.txt").write_text(TEXT_IN_A, encoding="utf-8")
    link = world / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("this system cannot create symbolic links")
    await _add_project("proj-l", link)
    await seed("chat-l", project_id="proj-l")
    client = _client(_reply(READ_NOTE))
    await _run(client, "chat-l")
    assert TEXT_IN_A not in _results(client)


async def test_the_parent_context_is_left_alone(world):
    session_id, _ = await _bound(world)
    client = _client(_reply(READ_NOTE))
    parent = await _run(client, session_id, read_only=True)
    assert parent.get("project_id") is None
    assert parent.get("subagent_read_only") is None
    assert parent.get("subagent_run") is None
    assert parent.session_id == session_id


async def test_a_subagent_dispatched_by_a_heartbeat_gets_the_project_too(world):
    session_id, _ = await _bound(world)
    client = _client(_reply(READ_NOTE))
    await _run(client, session_id, "YOLO", heartbeat_run=True, enable_autonomy=True)
    assert TEXT_IN_A in _results(client)


# ── 模式 ─────────────────────────────────────────────────────────


WRITE_NEW = ("write", {"scope": "project", "path": "new.txt", "content": "written"})
DELETE_NOTE = ("delete", {"scope": "project", "path": "note.txt"})


@pytest.mark.parametrize(("mode", "allowed"), [("Manual", False), ("Auto", True), ("YOLO", True)])
async def test_writing_follows_the_conversation_mode(world, mode, allowed):
    session_id, root = await _bound(world)
    await _run(_client(_reply(WRITE_NEW)), session_id, mode)
    assert (root / "new.txt").exists() is allowed


@pytest.mark.parametrize(("mode", "allowed"), [("Manual", False), ("Auto", False), ("YOLO", True)])
async def test_deleting_needs_yolo_because_nobody_is_there_to_approve(world, mode, allowed):
    session_id, root = await _bound(world)
    await _run(_client(_reply(DELETE_NOTE)), session_id, mode)
    assert (root / "note.txt").exists() is not allowed


# ── 只能看 ───────────────────────────────────────────────────────


async def test_a_read_only_subagent_only_sees_tools_that_read(world):
    session_id, _ = await _bound(world)
    client = _client()
    await _run(client, session_id, "YOLO", read_only=True)
    advertised = _advertised(client)
    assert set(READS) <= advertised
    assert not set(CHANGES) & advertised


@pytest.mark.parametrize("mode", ["Manual", "Auto", "YOLO"])
async def test_a_read_only_subagent_cannot_change_files_in_any_mode(world, mode):
    session_id, root = await _bound(world)
    legacy_write = (
        "project_tools",
        {"action": "write", "path": "legacy.txt", "content": "written"},
    )
    client = _client(_reply(WRITE_NEW), _reply(DELETE_NOTE), _reply(legacy_write))
    await _run(client, session_id, mode, read_only=True)
    assert not (root / "new.txt").exists()
    assert not (root / "legacy.txt").exists()
    assert (root / "note.txt").exists()


@pytest.mark.parametrize("mode", ["Manual", "Auto", "YOLO"])
async def test_a_read_only_subagent_can_still_look_at_the_project(world, mode):
    session_id, _ = await _bound(world)
    client = _client(
        _reply(READ_NOTE),
        _reply(("list", {"scope": "project"})),
        _reply(("find", {"scope": "project", "pattern": "*.txt"})),
        _reply(("grep", {"scope": "project", "query": "only-in"})),
    )
    await _run(client, session_id, mode, read_only=True)
    results = _results(client)
    assert TEXT_IN_A in results
    assert "note.txt" in results


async def test_the_dispatch_tool_carries_the_limit_all_the_way(world):
    session_id, root = await _bound(world)
    client = _client(_reply(WRITE_NEW))
    parent = _parent(client, session_id, "YOLO")
    result = await subagent_tools.dispatch_subagent(
        goal="只看", identity=A, request_id="parent", model=MODEL, _ctx=parent, read_only=True
    )
    assert "callback_token" in result
    await _settle()
    assert "write" not in _advertised(client)
    assert not (root / "new.txt").exists()


async def test_the_limit_also_holds_when_a_listed_tool_is_called(world):
    """工具清單之外的第二道：就算 write 在清單上，執行時的決策表也要擋。"""
    session_id, root = await _bound(world)
    client = _client(_reply(WRITE_NEW))
    parent = _parent(client, session_id, "YOLO")
    writable = subagent_tools.SubagentScope("proj-a", has_project=True)
    limited = subagent_tools.SubagentScope("proj-a", has_project=True, read_only=True)
    request = await subagent_tools._assemble_subagent_request(
        goal="照指示做", sub_identity=SUB, base_ctx=parent, model=MODEL, scope=writable
    )
    assert "write" in {tool["function"]["name"] for tool in request.tools}
    await subagent_tools._run_assembled(request, SUB, "parent:sub", parent, "照指示做", limited)
    assert not (root / "new.txt").exists()


async def test_each_subagent_in_a_group_has_its_own_limit(world):
    session_id, root = await _bound(world)
    replies = [_reply(WRITE_NEW), _reply(text="一"), _reply(WRITE_NEW), _reply(text="二")]
    tools_seen: list[set[str]] = []

    async def create(**kwargs: Any) -> MagicMock:
        tools_seen.append({tool["name"] for tool in kwargs.get("tools", [])})
        return replies.pop(0)

    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=create)
    results = await subagent_tools.run_subagent_group(
        subtasks=[{"goal": "只看", "read_only": True}],
        parent_identity=A,
        parent_request_id="parent",
        base_ctx=_parent(client, session_id, "YOLO"),
        model=MODEL,
    )
    assert results[0]["status"] == "DONE"
    assert not (root / "new.txt").exists()
    assert "write" not in tools_seen[0]

    results = await subagent_tools.run_subagent_group(
        subtasks=[{"goal": "可以寫"}],
        parent_identity=A,
        parent_request_id="parent",
        base_ctx=_parent(client, session_id, "YOLO"),
        model=MODEL,
    )
    assert results[0]["status"] == "DONE"
    assert (root / "new.txt").exists()


def _decision(tool: str, mode: str, *, read_only: bool, action: str | None = None) -> str:
    context = PolicyContext(
        actor=SUB,
        resource=tool,
        action="tool_call",
        environment={"auth_level": mode, "read_only_run": read_only, "tool_action": action},
    )
    return permission_service.policies.evaluate(context).effect


@pytest.mark.parametrize("mode", ["Manual", "Auto", "YOLO"])
@pytest.mark.parametrize("tool", [*CHANGES, "tool-nobody-classified"])
def test_the_decision_table_refuses_anything_that_is_not_read_only(mode, tool):
    assert _decision(tool, mode, read_only=True) == "DENY"


@pytest.mark.parametrize("mode", ["Manual", "Auto", "YOLO"])
@pytest.mark.parametrize("tool", READS)
def test_the_decision_table_keeps_allowing_reads(mode, tool):
    assert _decision(tool, mode, read_only=True) == "ALLOW"


@pytest.mark.parametrize("mode", ["Manual", "Auto", "YOLO"])
def test_a_tool_with_read_and_write_actions_is_judged_by_the_action(mode):
    assert _decision("vault_tools", mode, read_only=True, action="write") == "DENY"
    assert _decision("vault_tools", mode, read_only=True, action="read") == "ALLOW"


def test_a_command_counts_as_changing_things():
    context = PolicyContext(
        actor=SUB,
        resource="ls",
        action="terminal_execute",
        environment={"auth_level": "YOLO", "read_only_run": True},
    )
    assert permission_service.policies.evaluate(context).effect == "DENY"


def test_without_the_limit_the_table_is_unchanged():
    assert _decision("write", "YOLO", read_only=False) == "ALLOW"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, False),
        (False, False),
        (True, True),
        ("false", True),
        ("true", True),
        (0, True),
        (1, True),
    ],
)
async def test_only_a_plain_false_or_nothing_means_writable(monkeypatch, given, expected):
    seen: dict[str, Any] = {}

    async def deliver(**kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(subagent_tools, "_run_and_deliver", deliver)
    result = await subagent_tools.dispatch_subagent(
        goal="做事", identity=A, request_id="r", _ctx=PipelineContext(), read_only=given
    )
    assert "callback_token" in result
    await _settle()
    assert seen["read_only"] is expected


async def _settle() -> None:
    while subagent_tools._BACKGROUND_TASKS:
        await next(iter(subagent_tools._BACKGROUND_TASKS))


# ── 給子代理的提示 ───────────────────────────────────────────────


def _prompt(client: MagicMock) -> str:
    content = client.messages.create.call_args_list[0].kwargs["messages"][0]["content"]
    return content if isinstance(content, str) else "".join(b.get("text", "") for b in content)


async def test_the_prompt_mentions_the_project_without_its_real_path(world):
    session_id, root = await _bound(world)
    client = _client()
    await _run(client, session_id)
    prompt = _prompt(client)
    assert subagent_tools.PROJECT_NOTE in prompt
    assert subagent_tools.READ_ONLY_NOTE not in prompt
    assert str(root) not in prompt


async def test_the_prompt_says_read_only_when_it_is(world):
    session_id, _ = await _bound(world)
    client = _client()
    await _run(client, session_id, read_only=True)
    assert subagent_tools.READ_ONLY_NOTE in _prompt(client)


async def test_a_conversation_without_a_real_project_gets_no_project_hint(world):
    await seed("chat-default", project_id="default")
    client = _client()
    await _run(client, "chat-default")
    assert subagent_tools.PROJECT_NOTE not in _prompt(client)
