"""專用空間被拒絕時的政策（ADR-0332）.

- ``refused`` 一律直接拒絕：任何模式（Manual、Auto、YOLO）都是 DENY，不跳卡、不提供降級；
  只影響要隔離的動作，已經是 DENY 的保留原本的原因。
- 拒絕訊息專用（模組層函式挑選，不在 ``execute_to_queue`` 加分支）：暫時的（掃描、探測、封鎖埠、
  執行器重啟）說可以稍後再試，其他說原因；不用通用的「不要再試這個工具」。
- 政策層級帶「這次有人在場」（``is_human_present``），預設沒有。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext, PolicyDecision
from src.models.safety import PROJECT_SPACE_DENY_PREFIX, _require_isolation
from src.services import tool_executor as executor_module
from src.services.isolation import policy as policy_module
from src.services.isolation.status import LevelDecision
from src.utils.tool_events import TOOL_RESULT

from tests.support.hook_wiring import SOUL, WAIT_S, Recorder, clean_approval_state, executor_with


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    yield from clean_approval_state(monkeypatch)


def _context(mode: str, refused: str | None, *, action: str = "terminal_execute") -> PolicyContext:
    return PolicyContext(
        actor=SOUL,
        resource="git status" if action == "terminal_execute" else "read",
        action=action,
        environment={"auth_level": mode, "isolation": "none", "isolation_refused": refused},
    )


@pytest.mark.parametrize("mode", ["Manual", "Auto", "YOLO", "Unknown"])
@pytest.mark.parametrize("effect", ["ALLOW", "CONFIRM", "HUMAN_ONLY"])
def test_a_refused_project_is_denied_in_every_mode(mode, effect):
    decision = _require_isolation(_context(mode, "scan_limit"), PolicyDecision(effect=effect))
    assert decision.effect == "DENY"
    assert decision.reason == f"{PROJECT_SPACE_DENY_PREFIX}scan_limit"
    assert decision.metadata["isolation_refused"] == "scan_limit"


def test_an_existing_deny_keeps_its_reason():
    original = PolicyDecision(effect="DENY", reason="Matched critical blacklist pattern: rm")
    assert _require_isolation(_context("YOLO", "scan_limit"), original) == original


@pytest.mark.parametrize("refused", [None, ""])
def test_without_a_refusal_manual_is_unchanged(refused):
    original = PolicyDecision(effect="CONFIRM", reason="x")
    assert _require_isolation(_context("Manual", refused), original) == original


def test_actions_that_do_not_need_isolation_are_not_touched():
    original = PolicyDecision(effect="ALLOW")
    context = _context("YOLO", "scan_limit", action="tool_call")
    assert _require_isolation(context, original) == original


def test_level_decision_exposes_the_refusal_reason_only_when_refused():
    assert LevelDecision("none", "scan_limit", refused=True).refused_reason == "scan_limit"
    assert LevelDecision("none", "project_not_in_executor").refused_reason is None
    assert LevelDecision("executor").refused_reason is None


GENERIC = (
    "Tool 'execute_command' was denied by policy for role 'Owner'.",
    "Do not retry this tool; explain the restriction to the user or use an allowed alternative.",
)


def test_other_denials_keep_the_generic_text():
    for reason in ("policy", "mcp_tool_rbac", "Matched critical blacklist pattern: rm", None):
        assert policy_module.denial_text("execute_command", "Owner", reason) == GENERIC


@pytest.mark.parametrize(
    "refused", ["scan_limit", "probe_pending", "block_ports_incomplete", "executor_unreachable"]
)
def test_temporary_refusals_say_it_may_work_later(refused):
    message, suggestion = policy_module.denial_text(
        "execute_command", "Owner", f"{PROJECT_SPACE_DENY_PREFIX}{refused}"
    )
    assert refused in message and "dedicated project space" in message
    assert "try again" in suggestion and "Do not retry this tool" not in suggestion


@pytest.mark.parametrize(
    "refused", ["project_not_top_level", "project_linked", "executor_project_root_mismatch"]
)
def test_permanent_refusals_say_why_and_not_to_retry(refused):
    message, suggestion = policy_module.denial_text(
        "execute_command", "Owner", f"{PROJECT_SPACE_DENY_PREFIX}{refused}"
    )
    assert refused in message
    assert "try again" not in suggestion and "Do not retry this tool;" not in suggestion
    assert "tell the user" in suggestion


@pytest.mark.parametrize(
    ("ctx", "attended"),
    [
        ({"resumable_stream": True}, True),
        ({"resumable_stream": True, "heartbeat_run": True}, False),
        ({"resumable_stream": True, "subagent_run": True}, False),
        ({}, False),
    ],
)
async def test_the_policy_level_says_whether_a_human_is_present(monkeypatch, ctx, attended):
    seen: list[dict[str, Any]] = []

    async def _ready():
        return None

    async def _level_for(identity, project_root, **kwargs):
        seen.append({"root": project_root, **kwargs})
        return LevelDecision("executor")

    async def _root(_ctx, _session):
        return Path("/app/workspace/touka-projects/repo")

    monkeypatch.setattr(policy_module.isolation_monitor, "ensure_ready", _ready)
    monkeypatch.setattr(policy_module.isolation_monitor, "level_for", _level_for)
    monkeypatch.setattr(policy_module.isolation_monitor, "executor_scope_issue", lambda *_: None)
    monkeypatch.setattr(policy_module, "session_project_root", _root)
    identity = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
    await policy_module.policy_level(
        identity, ctx, None, "call-1", tool="execute_command", args={"command": "ls"}
    )
    assert seen == [{"root": Path("/app/workspace/touka-projects/repo"), "attended": attended}]


async def test_the_executor_denies_a_refused_project_without_running_it(monkeypatch):
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"execute_command": recorder})

    async def _refused(*_args, **_kwargs):
        return LevelDecision("none", "probe_pending", refused=True)

    monkeypatch.setattr(executor_module, "policy_level", _refused)
    ctx: dict[str, Any] = {
        "identity": SOUL, "auth_level": "YOLO", "session_id": "s-space", "resumable_stream": True,
    }
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    async with asyncio.timeout(WAIT_S):
        result = await executor.execute_to_queue(
            "execute_command", {"command": "git status"}, "req-space", ctx, queue
        )
    assert recorder.calls == []
    assert "probe_pending" in result and "try again" in result
    assert "Do not retry this tool;" not in result
    items: list[dict[str, Any] | None] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    assert items[-1] is None and items.count(None) == 1
    assert any(item and item.get("type") == TOOL_RESULT for item in items)
