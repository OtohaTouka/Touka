"""每一輪說清楚模式、有沒有人在場、需要核准的會怎樣（ADR-0316，可視性第一批 A2）。

只要這一輪有列工具，ToolLayer 就注入 ``<approval_context>``；說法要和實際的權限判斷
對得上（同一組模式與在場條件跑一次判斷），被擋後的建議也照子代理、其他無人兩種情境寫。
全程不連網、不讀真資料。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from src.adapters.tool_schema import reorder_system_stable_first, to_cached_system
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext, PolicyDecision
from src.models.safety import AuthPoliciesConfig, SafetyRules, ToolRiskConfig
from src.services import approval_flow
from src.services.context_snapshot_service import BLOCK_META
from src.services.permission_service import permission_service
from src.services.prompt_blocks import (
    ALL_BLOCK_TAGS,
    APPROVAL_CONTEXT,
    REQUEST_TAIL_TAGS,
    STABLE_SYSTEM_TAGS,
    VALIDATION_PROTOCOL,
    VOLATILE_SYSTEM_TAGS,
    build_approval_context,
    build_harness_context,
    build_validation_protocol,
)
from src.services.tool_executor import ToolExecutor
from src.tools import terminal as terminal_tool
from src.utils.permission_grants import NameGrant
from src.utils.presence import blocked_call_suggestion, no_human_followup

_ACTOR = AgentIdentity.for_role(agent_id="ModeProbe", role="Owner", is_human=False)
_SUBAGENT = {"subagent_run": True}
_HEARTBEAT = {"heartbeat_run": True}
_PRESENT = {"resumable_stream": True}
_OLD_ADVICE = ("Auto or YOLO", "open ToukaUI", "interactive chat")


def _block(mode: str, present: bool, ctx: dict[str, Any] | None = None) -> str:
    return build_approval_context(
        mode, human_present=present, followup=no_human_followup(ctx or {})
    )


def _effect(
    mode: str,
    resource: str,
    *,
    present: bool,
    action: str = "tool_call",
    mcp: bool = False,
    isolation: str = "landlock",
) -> str:
    config = AuthPoliciesConfig(
        policies=SafetyRules(),
        tool_risk=ToolRiskConfig(
            default="dangerous", tools={"research": "read_only", "write": "safe"}
        ),
    )
    environment: dict[str, Any] = {
        "auth_level": mode,
        "human_present": present,
        "isolation": isolation,
    }
    if mcp:
        environment["mcp_server"] = "srv"
    context = PolicyContext(
        actor=_ACTOR, resource=resource, action=action, environment=environment
    )
    return config.evaluate(context).effect


# ── 區塊登記與分類 ─────────────────────────────────────────────────────


def test_tag_is_registered_as_a_stable_system_block() -> None:
    # 同一段對話裡不變：歸「會變」組會讓 Claude 多切一個快取斷點
    assert APPROVAL_CONTEXT in ALL_BLOCK_TAGS
    assert APPROVAL_CONTEXT in STABLE_SYSTEM_TAGS
    assert APPROVAL_CONTEXT not in VOLATILE_SYSTEM_TAGS
    assert APPROVAL_CONTEXT in BLOCK_META


def test_every_block_tag_has_exactly_one_tier() -> None:
    # 新加的系統說明一定要歸類：不變（可快取）、會變、或接在訊息尾端
    groups = (STABLE_SYSTEM_TAGS, VOLATILE_SYSTEM_TAGS, REQUEST_TAIL_TAGS)
    for tag in ALL_BLOCK_TAGS:
        assert sum(tag in group for group in groups) == 1, tag
    assert {tag for group in groups for tag in group} == set(ALL_BLOCK_TAGS)


# ── 說法和實際判斷一致 ─────────────────────────────────────────────────


def test_manual_with_a_human_pauses_for_approval() -> None:
    assert _effect("Manual", "write", present=True) == "CONFIRM"
    text = _block("Manual", True)
    assert "pause for human approval" in text
    assert "can answer approval cards" in text


def test_manual_without_a_human_denies_state_changes() -> None:
    assert _effect("Manual", "write", present=False) == "CONFIRM"  # 沒人能核准＝被拒
    assert _effect("Manual", "research", present=False) == "ALLOW"
    text = _block("Manual", False)
    assert "denied" in text
    assert "only read-only tools run" in text
    assert "Nobody can answer approval requests" in text


def test_auto_with_a_human_runs_low_risk_and_routes_the_rest() -> None:
    assert _effect("Auto", "write", present=True) == "ALLOW"
    assert _effect("Auto", "remote_tool", present=True) == "REVIEW"
    assert _effect("Auto", "ls", present=True, action="terminal_execute") == "REVIEW"
    text = _block("Auto", True)
    assert "low-risk actions run directly" in text
    assert "Auto reviewer" in text


def test_auto_without_a_human_uses_the_conservative_wording() -> None:
    assert _effect("Auto", "write", present=False) == "ALLOW"
    assert _effect("Auto", "remote_tool", present=False) == "REVIEW"  # 沒人時轉人＝被拒
    text = _block("Auto", False)
    assert "low-risk actions run directly" in text
    assert "may need a human and be denied" in text
    assert "do not retry" in text


def test_yolo_with_a_human_runs_without_per_step_approval() -> None:
    assert _effect("YOLO", "remote_tool", present=True, mcp=True) == "ALLOW"
    text = _block("YOLO", True)
    assert "without per-step approval" in text
    assert "only a human may approve" in text


def test_yolo_without_a_human_denies_dangerous_mcp_tools() -> None:
    assert _effect("YOLO", "remote_tool", present=False, mcp=True) == "HUMAN_ONLY"
    text = _block("YOLO", False)
    assert "dangerous MCP tools" in text
    assert "denied" in text


def test_yolo_without_isolation_makes_some_actions_human_only() -> None:
    effect = _effect(
        "YOLO", "ls", present=True, action="terminal_execute", isolation="none"
    )
    assert effect == "HUMAN_ONLY"
    assert "no isolated environment" in _block("YOLO", True)


def test_yolo_mentions_that_chained_commands_are_not_auto_run() -> None:
    assert "chain or embed other commands" in _block("YOLO", True)
    assert "chain or embed other commands" in _block("YOLO", False)
    assert "chain or embed" not in _block("Auto", True)


def test_unknown_mode_is_described_as_manual() -> None:
    assert _effect("SafeAuto", "write", present=True) == "CONFIRM"
    assert _block("SafeAuto", True) == _block("Manual", True)
    assert _block("SafeAuto", False) == _block("Manual", False)


def test_followup_depends_on_who_is_running() -> None:
    assert "report to the agent that dispatched you" in _block("Manual", False, _SUBAGENT)
    assert "mission board" in _block("Manual", False, _HEARTBEAT)


def test_block_text_has_no_per_turn_values() -> None:
    # 同一段對話裡內容不變（不放時間、輪數），不破壞快取
    assert _block("Auto", True) == _block("Auto", True)
    assert not any(ch.isdigit() for ch in _block("Auto", False, _HEARTBEAT))


# ── 模式說法只在一個地方 ───────────────────────────────────────────────


def test_validation_protocol_keeps_only_the_validation_rules() -> None:
    block = build_validation_protocol()
    assert f"<{VALIDATION_PROTOCOL}>" in block
    assert "VALIDATE" in block
    for mode in ("Manual mode", "Auto mode", "YOLO mode"):
        assert mode not in block


def test_harness_points_to_the_approval_block() -> None:
    block = build_harness_context(
        model="m", role="Owner", advertised=frozenset({"execute_command"})
    )
    assert f"{APPROVAL_CONTEXT} block" in block
    assert "may pause for human approval" not in block
    empty = build_harness_context(model="m", role="Owner", advertised=frozenset())
    assert APPROVAL_CONTEXT not in empty


def test_claude_system_keeps_one_cache_breakpoint_without_volatile_blocks() -> None:
    # 只有不變的說明時，system 只佔一個快取斷點（原生用戶端自己帶的斷點才不會超過 4 個）；
    # 說明裡也不能出現會變區塊的標籤字樣，否則重排會切開
    harness = build_harness_context(
        model="m", role="Owner", advertised=frozenset({"execute_command"})
    )
    system = "<persona>\nx\n</persona>" + harness + _block("Auto", True)

    reordered = reorder_system_stable_first(system, volatile_tags=VOLATILE_SYSTEM_TAGS)
    cached = to_cached_system(reordered, volatile_tags=VOLATILE_SYSTEM_TAGS)

    assert reordered == system
    assert len(cached) == 1


# ── ToolLayer：有列工具的每一輪都帶到 ─────────────────────────────────


def _perm(allowed: list[str]) -> MagicMock:
    perm = MagicMock()
    perm.get_allowed_tools.return_value = NameGrant.from_lists(allowed)
    perm.is_tool_allowed.side_effect = lambda name, grant: grant.allows(name)
    perm.is_mcp_server_allowed.return_value = True
    perm.evaluate_policy.side_effect = lambda _c: MagicMock(effect="ALLOW", reason=None)
    return perm


def _layer_ctx(**flags: Any) -> PipelineContext:
    ctx = PipelineContext(identity=AgentIdentity.for_role(agent_id="DebugAgent", role="Owner"))
    ctx["agent_id"] = "DebugAgent"
    for key, value in flags.items():
        ctx[key] = value
    return ctx


async def _layer_system(allowed: list[str], ctx: PipelineContext) -> str:
    request = SimpleNamespace(
        tools=None, project_id=None, ide_context=None, system=None,
        messages=[{"role": "user", "content": "hi"}],
    )
    with patch("src.middleware.layers.tool.permission_service", _perm(allowed)):
        await ToolLayer().process(request, ctx)
    parts = []
    for message in request.messages:
        is_dict = isinstance(message, dict)
        role = message.get("role") if is_dict else getattr(message, "role", "")
        if role == "system":
            parts.append(str(message.get("content") if is_dict else message.content))
    return "".join(parts)


async def test_read_only_tools_still_get_the_block() -> None:
    text = await _layer_system(["research"], _layer_ctx(resumable_stream=True))
    assert f"<{APPROVAL_CONTEXT}>" in text
    assert "Manual" in text
    assert "can answer approval cards" in text


async def test_no_listed_tools_means_no_block() -> None:
    text = await _layer_system([], _layer_ctx(resumable_stream=True))
    assert f"<{APPROVAL_CONTEXT}>" not in text


async def test_subagent_turn_says_nobody_can_approve() -> None:
    text = await _layer_system(
        ["research"], _layer_ctx(resumable_stream=True, subagent_run=True, auth_level="Auto")
    )
    assert "Nobody can answer approval requests" in text
    assert "report to the agent that dispatched you" in text


# ── 被擋後的建議：子代理交回主代理、其他無人寫進回覆或看板 ───────────────


def test_shared_suggestion_by_situation() -> None:
    assert "report to the agent that dispatched you" in blocked_call_suggestion(_SUBAGENT)
    assert "mission board" in blocked_call_suggestion(_HEARTBEAT)
    for ctx in (_SUBAGENT, _HEARTBEAT, {}, _PRESENT):
        assert not any(old in blocked_call_suggestion(ctx) for old in _OLD_ADVICE)


def test_suggestion_with_someone_present_does_not_say_wait_for_a_human() -> None:
    # 終端在 YOLO 串接指令時，有人在場也會走到被擋的路
    text = blocked_call_suggestion(_PRESENT)
    assert "when they return" not in text
    assert "tell the user" in text


@pytest.mark.parametrize("ctx", [_SUBAGENT, _HEARTBEAT], ids=["subagent", "heartbeat"])
def test_approval_flow_errors_use_the_given_suggestion(ctx: dict[str, Any]) -> None:
    suggestion = blocked_call_suggestion(ctx)
    for text in (
        approval_flow.human_only_unavailable_error("write", None, suggestion=suggestion),
        approval_flow.auto_review_no_human_error(
            "write", "Needs a human.", headless_terminal=False, suggestion=suggestion
        ),
    ):
        payload = json.loads(text)
        assert payload["suggestion"] == suggestion
        assert payload["reason"] in {"human_only", "auto_review_escalated"}


class _Recorder:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_kwargs: Any) -> str:
        self.calls += 1
        return json.dumps({"ok": True})


async def _execute(
    monkeypatch: pytest.MonkeyPatch, effect: str, flags: dict[str, Any], mode: str
) -> dict[str, Any]:
    recorder = _Recorder()
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect=effect, reason=f"test-{effect.lower()}"),
    )
    executor = ToolExecutor(
        protocol_manager=None,
        tool_registry={"write": recorder},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    ctx = {"identity": _ACTOR, "auth_level": mode, "session_id": None, **flags}
    result = await executor.execute_to_queue(
        "write", {"scope": "vault"}, "req-mode", ctx, asyncio.Queue()
    )
    assert recorder.calls == 0  # 拒絕本身不變
    return json.loads(result)


@pytest.mark.parametrize(
    ("effect", "mode", "reason"),
    [
        ("CONFIRM", "Manual", "approval_required"),
        ("HUMAN_ONLY", "YOLO", "human_only"),
        ("REVIEW", "Auto", "auto_review_escalated"),
    ],
)
@pytest.mark.parametrize("flags", [_SUBAGENT, _HEARTBEAT], ids=["subagent", "heartbeat"])
async def test_executor_denials_carry_the_situation_suggestion(
    monkeypatch: pytest.MonkeyPatch,
    effect: str,
    mode: str,
    reason: str,
    flags: dict[str, Any],
) -> None:
    payload = await _execute(monkeypatch, effect, flags, mode)
    assert payload["reason"] == reason
    assert payload["suggestion"] == blocked_call_suggestion(flags)


@pytest.mark.parametrize(
    "flags", [_SUBAGENT, _HEARTBEAT, None, _PRESENT], ids=["sub", "hb", "none", "present"]
)
async def test_headless_dangerous_command_suggestion(flags: dict[str, Any] | None) -> None:
    result = await terminal_tool.execute_command(
        command="mkdir dangerous-thing", identity=_ACTOR, auth_level="Manual", _ctx=flags
    )
    payload = json.loads(result)
    assert payload["reason"] == "ui_unavailable"
    assert payload["suggestion"] == blocked_call_suggestion(flags or {})



async def test_yolo_chained_command_is_not_auto_run_even_with_someone_present() -> None:
    # 說法的根據：終端把 YOLO 的串接指令升成要人核准，網頁聊天沒有那個通道，所以被擋
    result = await terminal_tool.execute_command(
        command="mkdir chain-a && mkdir chain-b",
        identity=_ACTOR,
        auth_level="YOLO",
        _ctx=_PRESENT,
    )
    payload = json.loads(result)
    assert payload["error_class"] == "permission_denied"
    assert payload["suggestion"] == blocked_call_suggestion(_PRESENT)
