"""ADR-0246 底線（U2c-1）：硬地板 F1–F7、HUMAN_ONLY 效果、規則檔寫壞時保留黑名單。

- 地板是程式常數，在 ``AuthPoliciesConfig.evaluate`` 最前面判斷；``auth_policies.json``
  沒有任何欄位能關掉它。F1–F4、F7 三種模式都只有人能核准（YOLO 也一樣）；黑名單的 DENY
  照舊優先（地板只會更嚴）。F5、F6 在 Auto 下只有人能核准（U2c-4），Manual、YOLO 照舊。
- ``HUMAN_ONLY``：有人在場跳卡；無人在場拒絕（終端也不落到遺留通道）；不在
  ``RUNNABLE_STRATEGIES``，terminal 只認人類審批卡核准後的可信 ``_hitl_preapproved``；
  互審驗證指令同樣逐條問人，無人在場走等人確認。
- 熱重載讀到壞的規則檔：黑名單保留上一份可用的，風險表退回最嚴的預設。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext, PolicyDecision
from src.models.safety import AuthPoliciesConfig
from src.services import tool_executor as executor_module
from src.services.approval_service import approval_service
from src.services.identity_router import get_verified_identity
from src.services.permission_service import PermissionService, permission_service
from src.services.review.evidence import policy_command_gate
from src.services.tool_executor import ToolExecutor
from src.tools import terminal as terminal_tool
from src.tools.builtin import tool_registry as builtin_registry
from src.utils.command_safety import RUNNABLE_STRATEGIES
from src.utils.hard_floor import classify_hard_floor
from src.utils.permission_grants import NameGrant

_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "auth_policies.json.example"
)
_ACTOR = AgentIdentity.for_role(agent_id="FloorProbe", role="Owner", is_human=False)
# 中性前綴（不是部署路徑）：這裡的判斷只看路徑裡的名稱，不看前綴。
_APP = "/ctr"

_SHELL_CASES: dict[str, list[str]] = {
    "F1": [
        "mv notes.txt ../../legacy/old.db",
        f"rm -rf {_APP}/data/soul/Alice",
        "Remove-Item -Recurse D:\\Touka\\data\\soul",
        "echo hacked > ../../soul/Alice/persona.md",
    ],
    "F2": [
        "cat .env",
        "type ..\\..\\.env.local",
        "cat /proc/1/environ",
        "cp users.json leak.txt",
        f"grep KEY {_APP}/host_root/.env",
    ],
    "F3": [
        "echo {} > auth_policies.json",
        f"cp evil.json {_APP}/config/permissions.json",
        "sed -i s/Manual/FullYOLO/ identity_map.json",
        "Set-Content autonomy.json '{}'",
        "echo {} > hooks.json",  # ADR-0292
        "cp evil.py ToukaProxy/config/hooks/guard.py",
    ],
    "F4": [
        "sed -i s/a/b/ ToukaProxy/src/models/safety.py",
        f"echo x >> {_APP}/host_root/ToukaChat/src/app.tsx",
    ],
    "F7": [
        "cd x; shutdown -h now",
        "ls && /sbin/reboot",
        "rm -fr /",
        "rm -r -f /etc",
        "Remove-Item -Recurse -Force C:\\",
        "docker compose down",
        "docker-compose -f compose.yml stop",
        "echo ok | dd of=/dev/sda",
        'bash -c "poweroff"',
        "cmd /c shutdown /s /t 0",
        "powershell -Command Stop-Computer",
        "mkfs.ext4 /dev/sdb1",
        "format c:",
    ],
}
_SHELL_PARAMS = [(cat, cmd) for cat, cmds in _SHELL_CASES.items() for cmd in cmds]

_NOT_FLOOR = [
    "ls -la",
    "cat notes.txt",
    'git commit -m "fix shutdown handler"',
    "echo shutdown",
    "rm -rf build/",
    "python analyze.py data/input.csv",
    "cat .env.example",
    "cat ../../soul/Alice/persona.md",
    "pytest tests/",
    "docker ps",
]


def _example_config(**overrides: Any) -> AuthPoliciesConfig:
    data = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    data.update(overrides)
    return AuthPoliciesConfig.model_validate(data)


def _terminal(config: AuthPoliciesConfig, level: str, command: str) -> PolicyDecision:
    return config.evaluate(
        PolicyContext(
            actor=_ACTOR,
            resource=command,
            action="terminal_execute",
            environment={"auth_level": level, "isolation": "landlock"},
        )
    )


def _tool(config: AuthPoliciesConfig, level: str, tool: str, action: str | None = None):
    env: dict[str, Any] = {"auth_level": level}
    if action is not None:
        env["tool_action"] = action
    return config.evaluate(
        PolicyContext(actor=_ACTOR, resource=tool, action="tool_call", environment=env)
    )


# ── 純函式分類 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("category", "command"), _SHELL_PARAMS)
def test_shell_floor_categories(category: str, command: str):
    assert classify_hard_floor("terminal_execute", command) == category


@pytest.mark.parametrize("command", _NOT_FLOOR)
def test_ordinary_commands_are_not_floor(command: str):
    assert classify_hard_floor("terminal_execute", command) is None


@pytest.mark.parametrize(
    ("tool", "action", "expected"),
    [
        ("request_skill_install", None, "F5"),
        ("connect_mcp_server", None, "F5"),
        ("create_mcp", None, "F5"),
        ("evolution_implement_and_verify", None, "F5"),
        ("edit_soul", None, "F5"),
        ("update_my_soul", None, "F5"),
        ("agent_control", "switch_brain", "F5"),
        ("send_message", None, "F6"),
        ("sns_control", "send_message", "F6"),
        ("agent_control", "list_brains", None),
        ("write", None, None),
        ("read", None, None),
    ],
)
def test_tool_floor_by_name_and_verb(tool: str, action: str | None, expected: str | None):
    assert classify_hard_floor("tool_call", tool, tool_action=action) == expected


def test_mcp_tool_with_native_name_is_not_classified():
    """MCP 工具只有名字和原生工具撞名，不算 F5／F6（原生工具才以名字判斷）。"""
    assert classify_hard_floor("tool_call", "send_message", is_mcp=True) is None


def test_shell_text_never_classified_as_tool_floor():
    assert classify_hard_floor("terminal_execute", "send_message") is None


# ── evaluate：地板在最前面、設定檔關不掉、只會更嚴 ───────────────────────────────

_HARD_COMMANDS = [cmds[0] for cat, cmds in _SHELL_CASES.items()]


@pytest.mark.parametrize("command", _HARD_COMMANDS)
@pytest.mark.parametrize("level", ["Manual", "Auto", "YOLO", "Wat"])
def test_hard_floor_is_human_only_in_every_mode_including_yolo(level: str, command: str):
    decision = _terminal(_example_config(), level, command)
    assert decision.effect == "HUMAN_ONLY"
    assert decision.metadata["hard_floor"] in {"F1", "F2", "F3", "F4", "F7"}


@pytest.mark.parametrize("command", _HARD_COMMANDS)
def test_read_only_mode_keeps_deny_for_floor_commands(command: str):
    """ReadOnly 已退役，走到引擎當 Manual：地板指令只有人能核准（事先核准的變寬 ③）。"""
    assert _terminal(_example_config(), "ReadOnly", command).effect == "HUMAN_ONLY"


@pytest.mark.parametrize("command", ["rm -rf /", "shutdown now"])
@pytest.mark.parametrize("level", ["Manual", "Auto", "YOLO"])
def test_blacklist_deny_still_wins_over_floor(level: str, command: str):
    decision = _terminal(_example_config(), level, command)
    assert decision.effect == "DENY"
    assert decision.metadata["hard_floor"] == "F7"


def test_floor_runs_before_whitelist_auto_allow():
    """``cat`` 原本在範本白名單（SafeAuto 自動放行）；白名單拿掉後 Auto 一律送審查員，
    地板先判斷，所以讀 .env 只有人能核准。"""
    config = _example_config()
    assert _terminal(config, "Auto", "cat notes.txt").effect == "REVIEW"
    assert _terminal(config, "Auto", "cat .env").effect == "HUMAN_ONLY"
    assert _terminal(config, "YOLO", "cd x; shutdown -h now").effect == "HUMAN_ONLY"


def test_policy_file_cannot_disable_the_floor():
    """白名單全開、黑名單清空、工具全唯讀、外加看似開關的欄位：地板照擋。"""
    config = _example_config(
        policies={"safe_whitelist": [".*"], "critical_blacklist": []},
        tool_risk={"default": "read_only", "tools": {}},
        hard_floor=False,
        disable_hard_floor=True,
    )
    for level in ("Auto", "YOLO"):
        assert _terminal(config, level, "cat .env").effect == "HUMAN_ONLY"
        assert _terminal(config, level, "rm -fr /").effect == "HUMAN_ONLY"
    assert "hard_floor" not in AuthPoliciesConfig.model_fields


@pytest.mark.parametrize(
    ("level", "tool", "action", "expected", "category"),
    [
        ("Manual", "edit_soul", None, "CONFIRM", "F5"),
        ("Manual", "send_message", None, "CONFIRM", "F6"),
        ("Auto", "request_skill_install", None, "HUMAN_ONLY", "F5"),
        ("Auto", "agent_control", "switch_brain", "HUMAN_ONLY", "F5"),
        ("Auto", "send_message", None, "HUMAN_ONLY", "F6"),
        ("YOLO", "edit_soul", None, "ALLOW", "F5"),
        ("YOLO", "agent_control", "switch_brain", "ALLOW", "F5"),
        ("YOLO", "send_message", None, "ALLOW", "F6"),
    ],
)
def test_f5_f6_return_category_but_keep_current_effect(level, tool, action, expected, category):
    decision = _tool(_example_config(), level, tool, action)
    assert decision.effect == expected
    assert decision.metadata["hard_floor"] == category


def test_non_floor_decision_has_no_floor_metadata():
    decision = _tool(_example_config(), "YOLO", "write")
    assert decision.effect == "ALLOW"
    assert "hard_floor" not in decision.metadata


# ── HUMAN_ONLY 在執行層 ─────────────────────────────────────────────────────────


def test_human_only_is_not_a_runnable_strategy():
    assert "HUMAN_ONLY" not in RUNNABLE_STRATEGIES


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps({"ok": True})


@pytest.fixture(autouse=True)
def _clean_broker():
    approval_service.reset_for_test()
    yield
    approval_service.reset_for_test()


def _executor(
    monkeypatch: pytest.MonkeyPatch, registry: dict[str, Any], effect: str = "HUMAN_ONLY"
) -> ToolExecutor:
    bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
    for name, handler in registry.items():
        if name in bindings:
            bindings[name] = replace(bindings[name], handler=handler)
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect=effect, reason="Hard floor F2"),
    )
    return ToolExecutor(
        protocol_manager=None, tool_registry=registry, memory_tool_registry={}, plugin_registry={},
        enforce_advertised=False,
    )


def _ctx(*, interactive: bool) -> dict[str, Any]:
    ctx: dict[str, Any] = {"identity": _ACTOR, "auth_level": "YOLO", "session_id": "s-floor"}
    if interactive:
        ctx["resumable_stream"] = True
    return ctx


async def _run_interactive(
    executor: ToolExecutor, name: str, args: dict[str, Any], *, approved: bool
) -> tuple[str, list[dict[str, Any]]]:
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        executor.execute_to_queue(name, args, "req-floor", _ctx(interactive=True), queue)
    )
    exec_id = None
    for _ in range(200):
        pending = list(approval_service._pending)
        if pending:
            exec_id = pending[0]
            break
        await asyncio.sleep(0.005)
    assert exec_id, "HUMAN_ONLY 有人在場必須跳卡"
    await approval_service.resolve(exec_id, approved=approved, resolver="tester")
    result = await task
    events = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)
    return result, events


@pytest.mark.asyncio
async def test_human_only_interactive_shows_card_and_runs_only_after_approval(monkeypatch):
    recorder = _Recorder()
    executor = _executor(monkeypatch, {"write": recorder})
    _result, events = await _run_interactive(executor, "write", {"scope": "vault"}, approved=True)
    assert len(recorder.calls) == 1
    types = [e.get("type") for e in events]
    assert types.index("tool_approval_request") < types.index("tool_start")


@pytest.mark.asyncio
async def test_human_only_terminal_approved_carries_trusted_preapproval(monkeypatch):
    recorder = _Recorder()
    executor = _executor(monkeypatch, {"execute_command": recorder})
    await _run_interactive(executor, "execute_command", {"command": "cat .env"}, approved=True)
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["_hitl_preapproved"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("write", {"scope": "vault"}),
        ("execute_command", {"command": "cat .env", "_hitl_preapproved": True}),
    ],
)
async def test_human_only_interactive_rejected_never_runs(monkeypatch, name, args):
    recorder = _Recorder()
    executor = _executor(monkeypatch, {name: recorder})
    result, _events = await _run_interactive(executor, name, args, approved=False)
    assert recorder.calls == []
    assert json.loads(result)["reason"] == "approval_rejected"


@pytest.mark.asyncio
async def test_model_supplied_preapproval_is_overwritten_by_trusted_context(monkeypatch):
    """模型在參數裡帶 ``_hitl_preapproved`` 無效：可信執行情境一律覆寫成 executor 的值。"""
    recorder = _Recorder()
    executor = _executor(monkeypatch, {"execute_command": recorder}, effect="ALLOW")
    queue: asyncio.Queue = asyncio.Queue()
    await executor.execute_to_queue(
        "execute_command",
        {"command": "cat .env", "_hitl_preapproved": True},
        "req-floor-m",
        _ctx(interactive=False),
        queue,
    )
    assert recorder.calls[0]["_hitl_preapproved"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args"), [("write", {"scope": "vault"}), ("execute_command", {"command": "cat .env"})]
)
async def test_human_only_headless_is_denied_even_for_terminal(monkeypatch, name, args):
    """無人在場一律拒絕；終端不像 CONFIRM 那樣落到遺留通道，工具本體完全沒被呼叫。"""
    recorder = _Recorder()
    executor = _executor(monkeypatch, {name: recorder})
    queue: asyncio.Queue = asyncio.Queue()
    result = await executor.execute_to_queue(
        name, args, "req-floor-h", _ctx(interactive=False), queue
    )
    payload = json.loads(result)
    assert recorder.calls == []
    assert payload["error_class"] == "permission_denied"
    assert payload["reason"] == "human_only"
    assert approval_service.pending_count() == 0


def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> list:
    ran: list = []

    async def _fake_core(command, *_a, **_k):
        ran.append(command)
        return 0, "ok", 1.0

    monkeypatch.setattr(terminal_tool, "_execute_core", _fake_core)
    return ran


@pytest.mark.asyncio
async def test_terminal_runs_floor_command_only_with_trusted_preapproval(monkeypatch):
    """真實政策：``cat .env`` 是 F2；沒有人類核准就擋，executor 的可信核准才執行。"""
    ran = _no_spawn(monkeypatch)
    blocked = await terminal_tool.execute_command(
        command="cat .env", identity=_ACTOR, auth_level="YOLO"
    )
    assert json.loads(blocked)["reason"] == "policy_block"
    for fake in ("yes", 1):
        await terminal_tool.execute_command(
            command="cat .env", identity=_ACTOR, auth_level="YOLO", _hitl_preapproved=fake
        )
    assert ran == []
    await terminal_tool.execute_command(
        command="cat .env", identity=_ACTOR, auth_level="YOLO", _hitl_preapproved=True
    )
    assert ran == ["cat .env"]


@pytest.mark.asyncio
async def test_terminal_preapproval_never_unblocks_deny(monkeypatch):
    ran = _no_spawn(monkeypatch)
    monkeypatch.setattr(terminal_tool, "_classify_command_strategy", lambda *_a, **_k: "DENY")
    result = await terminal_tool.execute_command(
        command="rm -rf /", identity=_ACTOR, auth_level="YOLO", _hitl_preapproved=True
    )
    assert ran == []
    assert json.loads(result)["reason"] == "policy_block"


# ── 互審驗證指令：HUMAN_ONLY 逐條問人 ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("approved", None), ("rejected", "rejected by the human")],
)
async def test_review_gate_asks_human_for_floor_command(verdict, expected):
    asked: list[str] = []

    async def _approver(cmd: str, **_card: object) -> str:
        asked.append(cmd)
        return verdict

    gate = policy_command_gate(_ACTOR, auth_level="YOLO", approver=_approver)
    assert await gate("cat .env") == expected
    assert asked == ["cat .env"]


@pytest.mark.asyncio
async def test_review_gate_without_human_waits_for_one():
    gate = policy_command_gate(_ACTOR, auth_level="YOLO", approver=None)
    expected = "requires human approval, but no human is present to approve it"
    assert await gate("cat .env") == expected


# ── /v1/debug/execute：沒有人在場的旗標，YOLO 下地板指令照擋（已知改變）────


def test_debug_execute_denies_floor_command_even_in_fullyolo(monkeypatch):
    debugger = AgentIdentity.for_role(
        agent_id="DebugAgent", role="Debugger", is_human=True, capabilities=[]
    )
    ran = _no_spawn(monkeypatch)
    monkeypatch.setattr(
        permission_service, "get_allowed_tools", lambda _i: NameGrant.from_lists(["*"])
    )
    monkeypatch.setattr(app.state, "context", {"tool_registry": builtin_registry}, raising=False)
    app.dependency_overrides[get_verified_identity] = lambda: debugger
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            "/debug/execute",
            json={
                "tool_name": "execute_command",
                "args": {"command": "cat .env"},
                "auth_level": "YOLO",
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert json.loads(response.json()["result"])["reason"] == "human_only"
    assert ran == []


# ── 規則檔寫壞：保留上一份可用的黑名單 ───────────────────────────────────────────


def _service_with_policy(tmp_path: Path) -> tuple[PermissionService, Path]:
    policy_path = tmp_path / "auth_policies.json"
    shutil.copyfile(_EXAMPLE, policy_path)
    PermissionService.reset_singleton()
    service = PermissionService(
        config_path=str(tmp_path / "missing_permissions.json"),
        identity_path=str(tmp_path / "missing_identity.json"),
        policy_path=str(policy_path),
    )
    return service, policy_path


@pytest.mark.parametrize("broken", ["{invalid-json", '{"policies": "oops"}'])
def test_broken_policy_file_keeps_last_good_blacklist(tmp_path, caplog, broken):
    service, policy_path = _service_with_policy(tmp_path)
    assert len(service.policies.policies.critical_blacklist) == 13

    policy_path.write_text(broken, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        service.reload_all()

    kept = service.policies.policies
    assert len(kept.critical_blacklist) == 13
    assert "rm\\s+-rf\\s+/" in kept.critical_blacklist
    assert "safe_whitelist" not in type(kept).model_fields  # 白名單已拿掉，不會因此復活
    assert service.policies.tool_risk.class_for("write") == "dangerous"
    deny = service.policies.evaluate(
        PolicyContext(
            actor=_ACTOR,
            resource="sudo ls",
            action="terminal_execute",
            environment={"auth_level": "YOLO"},
        )
    )
    assert deny.effect == "DENY"
    assert any("AUDIT:" in r.getMessage() for r in caplog.records)
    PermissionService.reset_singleton()


def test_broken_policy_file_at_startup_uses_default(tmp_path):
    """啟動時沒有上一份可用的規則：照舊用預設（黑名單空，地板仍在）。"""
    policy_path = tmp_path / "auth_policies.json"
    policy_path.write_text("{invalid-json", encoding="utf-8")
    PermissionService.reset_singleton()
    service = PermissionService(
        config_path=str(tmp_path / "missing_permissions.json"),
        identity_path=str(tmp_path / "missing_identity.json"),
        policy_path=str(policy_path),
    )
    assert service.policies.policies.critical_blacklist == []
    assert _terminal(service.policies, "YOLO", "cat .env").effect == "HUMAN_ONLY"
    PermissionService.reset_singleton()
