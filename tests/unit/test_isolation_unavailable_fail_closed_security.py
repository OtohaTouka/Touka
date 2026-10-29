"""隔離不可用時一律不執行（iso-c2 U1e）：終端、互審驗證指令、審批卡與同意.

- 層級 none、沒有人的同意 → 不 spawn，回 ``upstream_error``（原因 ``isolation_unavailable``）；
- 舊審批通道（ToukaUI）在 none 時直接拒絕，不問人；
- 政策時的層級掉了、卡片只寫「隔離降級」卻只剩直接執行 → 不執行；
- 預期 executor 時，沒有同意不改用 Landlock；
- 同意只能用一次；
- 審批卡帶 ``isolation``（level／fallback／features／reason／reason_detail；
  fallback 只在 none 時送）；
  核准沒帶 ``isolation_ack`` 或值和卡片的 fallback 不同 → 不算同意、不執行。
- 要隔離卻沒接 runner 的工具以合成工具驗證：卡片一律 ``direct``
  （本機 Landlock 正常、判定出錯、帳本作廢都一樣）。
monitor 用 ``PinnedMonitor``、broker 用真的 ``RunnerBroker``，spawn 換成記錄替身；不打網路。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
import src.routers.terminal as terminal_router
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services import approval_flow
from src.services.approval_flow import CommandApprover
from src.services.approval_service import approval_service
from src.services.identity_router import get_verified_identity
from src.services.isolation import agent_command
from src.services.isolation import policy as isolation_policy
from src.services.isolation.consent import ConsentKey, consent_ledger
from src.services.isolation.policy import policy_level
from src.services.isolation.status import IsolationSnapshot, LandlockState, isolation_monitor
from src.services.review import evidence, review_loop
from src.tools import terminal
from src.utils.async_db import session_service
from src.utils.isolation_policy import landlock_features

from tests.support.isolation import fake_kernel_layout, install_real_broker, pinned_snapshot

if TYPE_CHECKING:
    from pathlib import Path

    from memory_server.utils.zone_paths import ZonePath

    from tests.support.isolation import PinnedMonitor

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
ADMIN = AgentIdentity.for_role(agent_id="AdminU1e", role="admin", is_human=True)
COMMAND = "ls"
ARGS = {"command": COMMAND, "env": "powershell"}
KEY = ConsentKey("Alice", "s1", "c1")
# 執行器連不上、本機 Landlock 仍可用（ABI 7）：真的 monitor 降級給 landlock（有比對測試）
NONE = pinned_snapshot("none", reason="executor_unreachable", expected="executor")
NONE_WITHOUT_LANDLOCK = pinned_snapshot(
    "none", local_status="unavailable", abi=0, reason="selftest_failed"
)
# 真的代理人指令 profile（conftest 的替身在每個測試開始時才裝上）
REAL_COMMAND_PROFILE = agent_command.build_command_profile
UNWIRED = ["unwired_probe"]


@pytest.fixture(autouse=True)
def _synthetic_unwired_tool(monkeypatch):
    """整個模組都加入合成未接線工具，驗證未接線的拒絕與卡片規則。"""
    monkeypatch.setattr(isolation_policy, "ISOLATION_REQUIRED_TOOLS",
                        isolation_policy.ISOLATION_REQUIRED_TOOLS | set(UNWIRED))
    monkeypatch.setattr(approval_flow, "ISOLATION_UNWIRED_TOOLS", frozenset(UNWIRED))


class _DoneProc:
    returncode = 0

    def __init__(self) -> None:
        self.stdout = self

    async def readline(self) -> bytes:
        return b""

    async def wait(self) -> int:
        return 0


@pytest.fixture(autouse=True)
def _clean_terminal_state(monkeypatch):
    monkeypatch.setattr(session_service, "get_session_by_id", AsyncMock(return_value=None))
    terminal.reset_async_state_for_test()
    yield
    terminal.reset_async_state_for_test()


@pytest.fixture
def spawned(monkeypatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _spawn(*argv: Any, **kwargs: Any) -> _DoneProc:
        calls.append((argv, kwargs))
        return _DoneProc()

    monkeypatch.setattr(terminal, "spawn_exec", _spawn)
    return calls


def _isolation(monkeypatch, tmp_path: Path, snap: IsolationSnapshot) -> PinnedMonitor:
    return install_real_broker(monkeypatch, snap, fake_kernel_layout(tmp_path))


async def _execute(**overrides: Any) -> str:
    values: dict[str, Any] = {
        "auth_level": "Manual",
        "_hitl_preapproved": True,
        "session_id": "s1",
        "_call_id": "c1",
        "_ctx": {"identity": ALICE},
    }
    values.update(overrides)
    return await terminal.execute_command(COMMAND, ALICE, **values)


async def _record_policy() -> None:
    """政策評估時記下層級與 subject（tool_executor 的接線方式）。"""
    await policy_level(ALICE, {"identity": ALICE}, "s1", "c1", tool="execute_command", args=ARGS)


def _assert_isolation_error(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    assert payload["error_class"] == "upstream_error", payload
    assert payload["reason"] == "isolation_unavailable", payload
    return payload


# ── 終端 ────────────────────────────────────────────────────────────────────────


async def test_none_without_consent_never_spawns(monkeypatch, tmp_path, spawned):
    _isolation(monkeypatch, tmp_path, NONE)
    payload = _assert_isolation_error(await _execute())
    assert payload["isolation_reason"] == "executor_unreachable"
    assert spawned == []


async def test_background_none_without_consent_never_spawns(monkeypatch, tmp_path, spawned):
    _isolation(monkeypatch, tmp_path, NONE)
    handle = json.loads(await _execute(background=True))
    record = terminal._ASYNC_RESULTS[handle["execution_id"]]
    await record["_task"]
    result = await terminal.get_command_result(handle["execution_id"], identity=ALICE)
    _assert_isolation_error(result)
    assert record["status"] == "error"
    assert spawned == []


async def test_legacy_channel_refuses_at_none_without_asking(monkeypatch, tmp_path, spawned):
    _isolation(monkeypatch, tmp_path, NONE)
    asked: list[str] = []

    async def _request_approval(exec_id: str, *_args: Any) -> tuple[bool, None]:
        asked.append(exec_id)
        return True, None

    monkeypatch.setattr(terminal, "_is_touka_ui_available", lambda: True)
    monkeypatch.setattr(terminal_router, "request_approval", _request_approval)
    _assert_isolation_error(await _execute(_hitl_preapproved=False))
    await asyncio.sleep(0)
    assert asked == [] and terminal._ASYNC_RESULTS == {}
    assert spawned == []


async def test_policy_level_dropped_before_spawn_is_refused(monkeypatch, tmp_path, spawned):
    monitor = _isolation(monkeypatch, tmp_path, pinned_snapshot("landlock"))
    await _record_policy()  # 卡片當時是 landlock，沒有標「未隔離」
    monitor.snap = NONE
    _assert_isolation_error(await _execute())
    assert spawned == []


async def test_degraded_card_with_only_direct_left_is_refused(monkeypatch, tmp_path, spawned):
    monitor = _isolation(monkeypatch, tmp_path, NONE)
    await _record_policy()
    assert consent_ledger.grant(KEY, fallback="landlock")  # 卡片寫「隔離降級」
    monitor.state_ok = False  # 執行時只剩直接執行
    _assert_isolation_error(await _execute())
    assert spawned == []


async def test_expected_executor_never_falls_back_to_landlock(monkeypatch, tmp_path, spawned):
    local = LandlockState("available", abi=7)
    snap = IsolationSnapshot(
        "none", "executor", "executor_unreachable", None, local, local.features
    )
    _isolation(monkeypatch, tmp_path, snap)
    payload = _assert_isolation_error(await _execute())
    assert payload["isolation_reason"] == "executor_unreachable"
    assert spawned == []


async def test_unisolated_consent_runs_direct_exactly_once(monkeypatch, tmp_path, spawned):
    _isolation(monkeypatch, tmp_path, NONE_WITHOUT_LANDLOCK)
    await _record_policy()
    assert consent_ledger.grant(KEY, fallback="direct")
    assert "[EXIT_CODE: 0]" in await _execute()
    (argv, _kwargs), = spawned
    assert not any(str(arg).endswith("launcher.py") for arg in argv)
    _assert_isolation_error(await _execute())  # 同一個同意不能再用
    assert len(spawned) == 1


def _sandbox_state_unset(monkeypatch, tmp_path: Path, *, landlock_usable: bool) -> None:
    """還沒重建（ST 沒設）：真的 profile 組不出來；monitor 是否認為 Landlock 降級可用由參數決定。"""
    layout = replace(fake_kernel_layout(tmp_path), state_root=None)
    drive = layout.data_root / "personal_drive" / "Alice"
    drive.mkdir(parents=True)
    snap = pinned_snapshot("none", reason="sandbox_state_unconfigured")
    install_real_broker(monkeypatch, snap, layout).state_ok = landlock_usable
    monkeypatch.setattr(agent_command, "build_command_profile", REAL_COMMAND_PROFILE)
    monkeypatch.setattr(agent_command, "current_layout", lambda: layout)
    monkeypatch.setattr(terminal, "_get_agent_drive_path", lambda _identity: drive)


@pytest.mark.parametrize("landlock_usable", [False, True])
async def test_unisolated_consent_runs_without_sandbox_state(
    monkeypatch, tmp_path, spawned, landlock_usable
):
    """人在「未隔離」卡上核准過：組不出 profile（ST 沒設）也照卡片直接執行，連 Manual 都不卡住。

    就算 monitor 這時認為 Landlock 可用，空的 profile 也不會被拿去當 Landlock 規則。
    """
    _sandbox_state_unset(monkeypatch, tmp_path, landlock_usable=landlock_usable)
    await _record_policy()
    assert consent_ledger.grant(KEY, fallback="direct")
    assert "[EXIT_CODE: 0]" in await _execute()
    (argv, _kwargs), = spawned
    assert not any(str(arg).endswith("launcher.py") for arg in argv)


async def test_degraded_consent_without_sandbox_state_is_refused_with_the_reason(
    monkeypatch, tmp_path, spawned
):
    """卡片寫「隔離降級」：組不出 profile 仍然不執行，原因照實帶出（不是籠統的不可用）。"""
    _sandbox_state_unset(monkeypatch, tmp_path, landlock_usable=True)
    await _record_policy()
    assert consent_ledger.grant(KEY, fallback="landlock")
    payload = _assert_isolation_error(await _execute())
    assert payload["isolation_reason"] == "sandbox_state_unconfigured"
    assert spawned == []


async def test_landlock_level_spawns_through_the_launcher(monkeypatch, tmp_path, spawned):
    _isolation(monkeypatch, tmp_path, pinned_snapshot("landlock"))
    assert "[EXIT_CODE: 0]" in await _execute()
    (argv, kwargs), = spawned
    launcher = str(fake_kernel_layout(tmp_path).kernel_dir / "launcher.py")
    assert argv[1:4] == ("-I", "-S", launcher)
    separator = argv.index("--")
    assert argv[separator - 4 : separator : 2] == ("--spec-fd", "--status-fd")
    assert kwargs["pass_fds"] == (int(argv[separator - 3]), int(argv[separator - 1]))
    assert argv[separator + 1 :][-1] == COMMAND


# ── 審批卡的隔離標示與 isolation_ack ──────────────────────────────────────────────


async def _open_card(
    events: list[dict[str, Any]], name: str = "execute_command", args: dict[str, Any] = ARGS
) -> tuple[asyncio.Task[str], dict[str, Any]]:
    """跳一張卡（預設 execute_command；和 tool_executor 同一個 approval_flow），回卡片事件。"""

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    task = asyncio.create_task(
        approval_flow.await_human_approval(
            name=name, args=args, call_id="c1", agent_id="Alice",
            session_id="s1", risk="dangerous", emit=_emit,
        )
    )
    async with asyncio.timeout(5):
        while not any(e["type"] == "tool_approval_request" for e in events):
            await asyncio.sleep(0.005)
    return task, next(e for e in events if e["type"] == "tool_approval_request")


async def _approve(card: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
    assert await approval_service.resolve(
        card["id"], approved=True, resolver="AdminU1e", response_payload=payload
    )


@pytest.fixture(autouse=True)
def _clean_broker():
    approval_service.reset_for_test()
    yield
    approval_service.reset_for_test()
    app.dependency_overrides.clear()


async def test_card_marks_degraded_isolation_and_approval_needs_the_ack(
    monkeypatch, tmp_path, spawned
):
    _isolation(monkeypatch, tmp_path, NONE)
    await _record_policy()
    events: list[dict[str, Any]] = []
    task, card = await _open_card(events)
    assert card["isolation"] == {
        "level": "none",
        "fallback": "landlock",
        "features": sorted(landlock_features(7)),
        "reason": "executor_unreachable",
        "reason_detail": None,
        "expected": "executor",
    }
    await _approve(card, {"isolation_ack": "landlock"})
    assert await task == "approved"
    assert "[EXIT_CODE: 0]" in await _execute()
    (argv, _kwargs), = spawned
    assert any(str(arg).endswith("launcher.py") for arg in argv)


async def test_card_marks_unisolated_with_the_state_detail(monkeypatch, tmp_path, spawned):
    snap = pinned_snapshot("none", reason="sandbox_state_unconfigured")
    _isolation(monkeypatch, tmp_path, IsolationSnapshot(
        snap.level, snap.expected, snap.reason, "not_writable", snap.local, snap.features
    )).state_ok = False
    await _record_policy()
    _task, card = await _open_card([])
    assert card["isolation"] == {
        "level": "none",
        "fallback": "direct",
        "features": [],
        "reason": "sandbox_state_unconfigured",
        "reason_detail": "not_writable",
        "expected": "landlock",
    }


@pytest.mark.parametrize("payload", [None, {}, {"isolation_ack": "landlock"}, {"ack": "direct"}])
async def test_approval_without_the_matching_ack_is_not_consent(
    monkeypatch, tmp_path, spawned, payload
):
    _isolation(monkeypatch, tmp_path, NONE_WITHOUT_LANDLOCK)
    await _record_policy()
    events: list[dict[str, Any]] = []
    task, card = await _open_card(events)
    assert card["isolation"]["fallback"] == "direct"
    await _approve(card, payload)
    assert await task == "isolation_unacknowledged"
    assert events[-1]["decision"] == "isolation_unacknowledged"
    denied = json.loads(approval_flow.approval_denied_error("execute_command", await task))
    assert denied["reason"] == "approval_isolation_unacknowledged"
    _assert_isolation_error(await _execute())
    assert spawned == []


async def test_degraded_card_cannot_turn_into_direct_execution(monkeypatch, tmp_path, spawned):
    monitor = _isolation(monkeypatch, tmp_path, NONE)
    await _record_policy()
    task, card = await _open_card([])
    await _approve(card, {"isolation_ack": card["isolation"]["fallback"]})
    assert await task == "approved"
    monitor.state_ok = False  # 卡片寫「隔離降級」，執行時只剩直接執行
    _assert_isolation_error(await _execute())
    assert spawned == []


async def test_landlock_card_needs_no_ack_and_has_no_fallback(monkeypatch, tmp_path, spawned):
    _isolation(monkeypatch, tmp_path, pinned_snapshot("landlock"))
    await _record_policy()
    task, card = await _open_card([])
    assert card["isolation"] == {
        "level": "landlock",
        "features": sorted(landlock_features(7)),
        "reason": None,
        "reason_detail": None,
        "expected": "landlock",
    }
    await _approve(card)
    assert await task == "approved"


async def test_ordinary_card_without_a_policy_record_is_unchanged(monkeypatch, tmp_path):
    _isolation(monkeypatch, tmp_path, NONE)
    task, card = await _open_card([])
    assert "isolation" not in card
    await _approve(card)
    assert await task == "approved"


async def test_approve_endpoint_refuses_a_missing_or_wrong_ack(monkeypatch, tmp_path):
    _isolation(monkeypatch, tmp_path, NONE_WITHOUT_LANDLOCK)
    await _record_policy()
    task, card = await _open_card([])
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/v1/tools/approve/{card['id']}"
    for body in (None, {"payload": {"isolation_ack": "landlock"}}):
        response = client.post(url, json=body)
        assert response.status_code == 422, response.text
        assert approval_service.get(card["id"]) is not None  # 卡片仍待決，沒有同意
    response = client.post(url, json={"payload": {"isolation_ack": "direct"}})
    assert response.status_code == 200, response.text
    assert await task == "approved"


# ── 互審驗證指令 ────────────────────────────────────────────────────────────────

VERIFY = "pytest -q"


class _VerifyProc:
    returncode = 0

    async def communicate(self) -> tuple[bytes, None]:
        return b"1 passed", None


@pytest.fixture
def verify_spawned(monkeypatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _spawn(*argv: Any, **kwargs: Any) -> _VerifyProc:
        calls.append((argv, kwargs))
        return _VerifyProc()

    monkeypatch.setattr(evidence, "spawn_exec", _spawn)
    return calls


async def _allow(_cmd: str) -> str | None:
    return None


async def _collect(drive: ZonePath, gate=_allow, consent=None):
    return await evidence.collect_evidence(
        artifact_refs=[], verification_cmd=VERIFY, test_log=None, artifact_root=drive,
        command_gate=gate, identity=ALICE, consent=consent,
    )


async def test_verify_none_without_consent_never_spawns(
    zone_drive, monkeypatch, tmp_path, verify_spawned
):
    _isolation(monkeypatch, tmp_path, NONE)
    bundle = await _collect(zone_drive)
    assert bundle.cmd_results[0].rejected_reason == evidence.NO_ISOLATED_ENVIRONMENT
    assert review_loop._HUMAN_GATE[evidence.NO_ISOLATED_ENVIRONMENT] == "isolation_unavailable"
    assert verify_spawned == []


async def test_verify_runs_through_the_launcher_at_landlock(
    zone_drive, monkeypatch, tmp_path, verify_spawned
):
    _isolation(monkeypatch, tmp_path, pinned_snapshot("landlock"))
    bundle = await _collect(zone_drive)
    assert bundle.cmd_results[0].exit_code == 0
    (argv, kwargs), = verify_spawned
    assert argv[3].endswith("launcher.py") and argv[-2:] == ("pytest", "-q")
    separator = argv.index("--")
    assert kwargs["pass_fds"] == (int(argv[separator - 3]), int(argv[separator - 1]))


async def _verify_with_card(monkeypatch, tmp_path, drive: ZonePath, ack: dict[str, Any] | None):
    _isolation(monkeypatch, tmp_path, NONE_WITHOUT_LANDLOCK)
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    approver = CommandApprover(
        emit=_emit, parent_call_id="r1", agent_id="Alice", session_id="s1",
        identity=ALICE, ctx={"identity": ALICE},
    )
    gate = evidence.policy_command_gate(ALICE, auth_level="Manual", approver=approver)
    task = asyncio.create_task(_collect(drive, gate, approver.take_consent))
    async with asyncio.timeout(5):
        while not any(e["type"] == "tool_approval_request" for e in events):
            await asyncio.sleep(0.005)
    card = next(e for e in events if e["type"] == "tool_approval_request")
    assert card["isolation"]["fallback"] == "direct"
    await _approve(card, ack)
    return await task, approver


async def test_verify_card_with_ack_runs_direct_once(
    monkeypatch, tmp_path, zone_drive, verify_spawned
):
    ack = {"isolation_ack": "direct"}
    bundle, approver = await _verify_with_card(monkeypatch, tmp_path, zone_drive, ack)
    assert bundle.cmd_results[0].exit_code == 0
    (argv, _kwargs), = verify_spawned
    assert argv == ("pytest", "-q")
    assert approver.take_consent(VERIFY) is None  # 同意已用掉


async def test_verify_card_without_ack_never_spawns(
    monkeypatch, tmp_path, zone_drive, verify_spawned
):
    bundle, _ = await _verify_with_card(monkeypatch, tmp_path, zone_drive, None)
    assert bundle.cmd_results[0].rejected_reason == evidence.NO_HUMAN_DECISION
    assert verify_spawned == []


async def test_card_offers_direct_when_the_project_cannot_be_granted(monkeypatch, tmp_path):
    _isolation(monkeypatch, tmp_path, NONE)  # 本機 Landlock 可用、ST 可用
    consent_ledger.record_policy(
        KEY, level="none", subject="s", reason="protected_location",
        project_root=tmp_path / "ToukaChat",
    )
    _task, card = await _open_card([])
    assert card["isolation"]["fallback"] == "direct"  # 專案授權不了，Landlock 降級跑不起來


# ── 沒接 runner 的工具：卡片依工具一律「未隔離」（對抗式第 2 輪 HIGH-1）─────────────────────

MCP_ARGS = {"name": "srv", "command": "sh", "args": ["-c", "id"]}
UNISOLATED_CARD = {
    "level": "none", "fallback": "direct", "features": [], "reason_detail": None,
    "expected": "landlock",
}


async def _record_unwired(tool: str) -> None:
    await policy_level(ALICE, {"identity": ALICE}, "s1", "c1", tool=tool, args=MCP_ARGS)


async def _check_raises() -> None:
    raise OSError


@pytest.mark.parametrize(
    ("ack", "verdict"), [("direct", "approved"), ("landlock", "isolation_unacknowledged")]
)
@pytest.mark.parametrize("tool", UNWIRED)
async def test_unwired_tool_card_is_unisolated_with_working_landlock(
    monkeypatch, tmp_path, tool, ack, verdict
):
    """不經 runner：本機 Landlock 正常也不能寫「隔離降級」；核准要送回 direct 才算同意。"""
    _isolation(monkeypatch, tmp_path, pinned_snapshot("landlock"))
    await _record_unwired(tool)
    task, card = await _open_card([], tool, MCP_ARGS)
    assert card["isolation"] == {**UNISOLATED_CARD, "reason": "runner_not_wired"}
    await _approve(card, {"isolation_ack": ack})
    assert await task == verdict


@pytest.mark.parametrize("tool", UNWIRED)
async def test_unwired_tool_card_is_unisolated_when_the_check_fails(monkeypatch, tmp_path, tool):
    """判定出錯也依工具判斷、不看原因字串：沒接 runner 一律「未隔離」。"""
    _isolation(monkeypatch, tmp_path, pinned_snapshot("landlock"))
    monkeypatch.setattr(isolation_monitor, "ensure_ready", _check_raises)
    await _record_unwired(tool)
    _task, card = await _open_card([], tool, MCP_ARGS)
    assert card["isolation"] == {**UNISOLATED_CARD, "reason": "isolation_check_failed"}


async def test_wired_tool_card_still_offers_landlock_when_the_check_fails(monkeypatch, tmp_path):
    """已接 runner 的終端照舊：判定出錯時照本機 Landlock 算降級去處。"""
    _isolation(monkeypatch, tmp_path, pinned_snapshot("landlock"))
    monkeypatch.setattr(isolation_monitor, "ensure_ready", _check_raises)
    await _record_policy()
    _task, card = await _open_card([])
    assert card["isolation"]["fallback"] == "landlock"
    assert card["isolation"]["reason"] == "isolation_check_failed"


@pytest.mark.parametrize("tool", ["create_mcp", "scaffold_mcp"])
async def test_creation_card_uses_working_landlock(monkeypatch, tmp_path, tool):
    _isolation(monkeypatch, tmp_path, pinned_snapshot("landlock"))
    await _record_unwired(tool)
    task, card = await _open_card([], tool, MCP_ARGS)
    assert card["isolation"]["level"] == "landlock"
    assert "fallback" not in card["isolation"]
    await _approve(card, {})
    assert await task == "approved"


@pytest.mark.parametrize("tool", UNWIRED)
async def test_unwired_tool_card_stays_unisolated_without_a_usable_record(
    monkeypatch, tmp_path, tool
):
    """同一個呼叫 id 先被終端記過（帳本作廢）：卡片仍寫「未隔離」，核准記不到同意、不算核准。"""
    _isolation(monkeypatch, tmp_path, pinned_snapshot("landlock"))
    await _record_policy()  # 例如同 id 的終端呼叫被政策拒絕後留下的紀錄
    await _record_unwired(tool)  # subject 不同 → 兩筆作廢
    task, card = await _open_card([], tool, MCP_ARGS)
    assert card["isolation"] == {**UNISOLATED_CARD, "reason": "runner_not_wired"}
    await _approve(card, {"isolation_ack": "direct"})
    assert await task == "isolation_unacknowledged"


class _StopReview(Exception):
    pass


async def test_review_round_hands_consent_identity_and_project_to_evidence(
    zone_drive, monkeypatch, tmp_path
):
    seen: dict[str, Any] = {}

    async def _collect(**kwargs: Any) -> None:
        seen.update(kwargs)
        raise _StopReview

    async def _emit(_event: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(review_loop, "collect_evidence", _collect)
    task = SimpleNamespace(
        id="t1", review_locked=False, artifact_refs=[], verification_cmd=VERIFY, test_log=None
    )
    manager = SimpleNamespace(
        get_current_mission=AsyncMock(return_value=SimpleNamespace(tasks=[task], metadata={})),
        record_review=AsyncMock(),
    )
    approver = CommandApprover(
        emit=_emit, parent_call_id="r1", agent_id="Alice", session_id="s1", identity=ALICE
    )
    with pytest.raises(_StopReview):
        await review_loop.run_review_round(
            task=task, goal="g", producer_claims="", artifact_root=zone_drive,
            parent_identity=ALICE, mission_id="m1", manager=manager, completer=None,
            critic_model=None, producer_model=None, approver=approver,
            project_root=tmp_path / "proj",
        )
    assert seen["consent"] == approver.take_consent
    assert seen["identity"] is ALICE and seen["project_root"] == tmp_path / "proj"
