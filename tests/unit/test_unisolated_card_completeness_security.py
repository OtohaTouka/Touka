"""未隔離卡的完整性（iso-c2 C2-3 必做 ③；對抗式第 2 輪①LOW 6）.

去處是「未隔離」（direct）的審批卡，人核准後程式會以 Touka 的全部權限執行，所以卡上必須看得到
要執行的全部內容：
- 每個參數值上卡的上限放到 64 KiB（一般參數原本 200 字、指令 8 KB）；
- 仍有值超過上限、或工具要執行的程式根本不在參數裡（``verify_mcp_locally``）時，卡片標
  ``complete: false``，**伺服器端**判 ``isolation_incomplete``：不算核准、不記同意（舊前端照送
  核准也一樣）；
- 去處是執行器或本機 Landlock（或層級本來就不是 none）的卡不受影響，截斷照舊可以核准；
- IDE 的「由人核准」標記照舊用原本的上限判斷有沒有截斷。
monitor 用 ``PinnedMonitor``，審批走真的 ``approval_service``；不 spawn、不打網路。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from src.models.identity import AgentIdentity
from src.services import approval_flow
from src.services.approval_flow import CommandApprover
from src.services.approval_service import approval_service
from src.services.isolation.consent import ConsentKey, consent_ledger, consent_subject
from src.utils.async_db import session_service

from tests.support.isolation import (
    PinnedMonitor,
    fake_kernel_layout,
    install_real_broker,
    pinned_snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

    from src.services.isolation.consent import Fallback
    from src.utils.isolation_policy import IsolationLevel

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
KEY = ConsentKey("Alice", "s1", "c1")
LIMIT = 64 * 1024
OLD_COMMAND_LIMIT = 8 * 1024
OLD_LIMIT = 200
# 執行器連不上、本機 Landlock 也不能用：降級去處只剩未隔離
DIRECT = pinned_snapshot("none", local_status="unavailable", abi=0, reason="selftest_failed")
# 執行器連不上、本機 Landlock 可用：降級去處是本機 Landlock
LANDLOCK = pinned_snapshot("none", reason="executor_unreachable", expected="executor")
EVOLVE = "evolution_implement_and_verify"
VERIFY_ARGS = {"skill_id": "weather", "tool_name": "get", "arguments": {"city": "Taipei"}}


@pytest.fixture(autouse=True)
def _clean_broker():
    approval_service.reset_for_test()
    yield
    approval_service.reset_for_test()


def _evolve_args(code: str) -> dict[str, Any]:
    return {
        "skill_id": "weather", "code": code, "dependencies": [], "test_tool_name": "get",
        "test_arguments": {},
    }


def _place(
    monkeypatch: pytest.MonkeyPatch, tool: str, args: dict[str, Any], *,
    snap=DIRECT, level: IsolationLevel = "none", executor: bool = False,
) -> None:
    """monitor 釘在 ``snap``，政策時記下層級（``executor``＝這次在執行器範圍、執行器可用）。"""
    features = frozenset({"fs"}) if executor else None
    monkeypatch.setattr(
        approval_flow, "isolation_monitor", PinnedMonitor(snap=snap, executor_features=features)
    )
    consent_ledger.record_policy(
        KEY, level=level, subject=consent_subject(tool, args), reason=snap.reason,
        executor_eligible=executor,
    )


async def _card(
    events: list[dict[str, Any]], tool: str, args: dict[str, Any]
) -> tuple[asyncio.Task[str], dict[str, Any]]:
    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    task = asyncio.create_task(
        approval_flow.await_human_approval(
            name=tool, args=args, call_id="c1", agent_id="Alice", session_id="s1",
            risk="dangerous", emit=_emit,
        )
    )
    async with asyncio.timeout(5):
        while not any(e["type"] == "tool_approval_request" for e in events):
            await asyncio.sleep(0.005)
    return task, next(e for e in events if e["type"] == "tool_approval_request")


async def _approve(card: dict[str, Any], ack: Fallback | None) -> None:
    payload = {"isolation_ack": ack} if ack else None
    assert await approval_service.resolve(
        card["id"], approved=True, resolver="AdminU3i", response_payload=payload
    )


def _consent(tool: str, args: dict[str, Any]):
    grant = consent_ledger.take(KEY, subject=consent_subject(tool, args))
    assert grant is not None
    return grant.consent


# ── 未隔離：64 KiB 內完整上卡、照舊可以核准 ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "args", "key"),
    [
        # 超過原本 8 KB 的指令、剛好 64 KiB 的指令、超過原本 200 字的程式碼
        ("execute_command", {"command": "x" * (OLD_COMMAND_LIMIT + 1), "env": "bash"}, "command"),
        ("execute_command", {"command": "x" * LIMIT, "env": "bash"}, "command"),
        (EVOLVE, _evolve_args("y" * 5000), "code"),
    ],
)
async def test_a_direct_card_shows_each_value_up_to_64_kib_and_can_be_approved(
    monkeypatch, tool, args, key
):
    _place(monkeypatch, tool, args)
    events: list[dict[str, Any]] = []
    task, card = await _card(events, tool, args)
    assert card["isolation"]["fallback"] == "direct"
    assert card["args"][key] == args[key]  # 完整上卡，沒有截斷標記
    assert "complete" not in card["isolation"]
    await _approve(card, "direct")
    assert await task == "approved"
    consent = _consent(tool, args)
    assert consent is not None and consent.fallback == "direct"


# ── 未隔離但卡上看不到全部：伺服器端不算核准、不記同意 ─────────────────────────────


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("execute_command", {"command": "x" * (LIMIT + 1), "env": "bash"}),
        (EVOLVE, _evolve_args("y" * (LIMIT + 1))),
        ("create_mcp", {"name": "srv", "command": "npx", "args": ["-y", "z" * LIMIT]}),
    ],
)
async def test_a_truncated_direct_card_is_refused_on_the_server_without_consent(
    monkeypatch, tool, args
):
    _place(monkeypatch, tool, args)
    events: list[dict[str, Any]] = []
    task, card = await _card(events, tool, args)
    assert card["isolation"]["fallback"] == "direct"
    assert card["isolation"]["complete"] is False
    # 舊前端照樣送核准（帶對的去處），後端仍拒絕
    await _approve(card, "direct")
    assert await task == "isolation_incomplete"
    assert events[-1]["type"] == "tool_approval_resolved"
    assert events[-1]["decision"] == "isolation_incomplete"
    assert _consent(tool, args) is None
    denied = json.loads(approval_flow.approval_denied_error(tool, "isolation_incomplete"))
    assert denied["error_class"] == "permission_denied"
    assert denied["reason"] == "approval_isolation_incomplete"
    assert "cannot show everything" in denied["message"]


async def test_verify_mcp_locally_on_a_direct_card_can_never_be_approved(monkeypatch):
    """要執行的程式（server.py）不在參數裡，卡上看不到：未隔離時一律不能核准。"""
    _place(monkeypatch, "verify_mcp_locally", VERIFY_ARGS)
    events: list[dict[str, Any]] = []
    task, card = await _card(events, "verify_mcp_locally", VERIFY_ARGS)
    assert card["isolation"]["fallback"] == "direct"
    assert card["isolation"]["complete"] is False
    await _approve(card, "direct")
    assert await task == "isolation_incomplete"
    assert _consent("verify_mcp_locally", VERIFY_ARGS) is None


async def test_a_verification_command_over_the_limit_is_refused(monkeypatch, tmp_path: Path):
    """互審的驗證指令走同一個 await_human_approval：未隔離而指令超過上限也不算核准。"""
    monkeypatch.setattr(session_service, "get_session_by_id", AsyncMock(return_value=None))
    install_real_broker(monkeypatch, DIRECT, fake_kernel_layout(tmp_path))
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    approver = CommandApprover(
        emit=_emit, parent_call_id="r1", agent_id="Alice", session_id="s1",
        identity=ALICE, ctx={"identity": ALICE},
    )
    command = "pytest " + "-k case " * (LIMIT // 8 + 1)
    task = asyncio.create_task(approver(command))
    async with asyncio.timeout(5):
        while not any(e["type"] == "tool_approval_request" for e in events):
            await asyncio.sleep(0.005)
    card = next(e for e in events if e["type"] == "tool_approval_request")
    assert card["isolation"]["fallback"] == "direct"
    assert card["isolation"]["complete"] is False
    await _approve(card, "direct")
    assert await task == "isolation_incomplete"
    grant = approver.take_consent(command)
    assert grant is not None and grant.consent is None  # 沒有同意：之後不會未隔離執行


# ── 去處是隔離（或層級不是 none）：截斷照舊可以核准 ─────────────────────────────────

ISOLATED_PLACES: list[tuple[str, dict[str, Any], Fallback | None]] = [
    ("landlock fallback", {"snap": LANDLOCK}, "landlock"),
    ("executor fallback", {"snap": LANDLOCK, "executor": True}, "executor"),
    ("isolated level", {"snap": pinned_snapshot("landlock"), "level": "landlock"}, None),
]


@pytest.mark.parametrize(("_label", "place", "ack"), ISOLATED_PLACES)
async def test_truncation_on_an_isolated_card_can_still_be_approved(
    monkeypatch, _label, place, ack
):
    args = _evolve_args("y" * (OLD_LIMIT + 100))
    _place(monkeypatch, EVOLVE, args, **place)
    task, card = await _card([], EVOLVE, args)
    assert card["isolation"].get("fallback") == ack
    assert card["args"]["code"].endswith("…(+100 chars)")  # 照原本的上限截斷
    assert "complete" not in card["isolation"]
    await _approve(card, ack)
    assert await task == "approved"
    consent = _consent(EVOLVE, args)
    assert (consent.fallback if consent else None) == ack


@pytest.mark.parametrize(("_label", "place", "ack"), ISOLATED_PLACES)
async def test_verify_mcp_locally_with_isolation_is_approved_as_before(
    monkeypatch, _label, place, ack
):
    _place(monkeypatch, "verify_mcp_locally", VERIFY_ARGS, **place)
    task, card = await _card([], "verify_mcp_locally", VERIFY_ARGS)
    assert "complete" not in card["isolation"]
    await _approve(card, ack)
    assert await task == "approved"


# ── IDE「由人核准」標記：照舊用原本的上限 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [{"code": "y" * (OLD_LIMIT + 1)}, {"command": "x" * (OLD_COMMAND_LIMIT + 1)}],
)
def test_the_ide_marker_keeps_the_original_preview_limits(args):
    """未隔離卡會完整顯示的值，對 IDE 標記仍算截斷（tool_executor 只用預設上限）。"""
    assert approval_flow.approval_preview_truncated(args) is True
    assert approval_flow.approval_args_preview(args) != args
