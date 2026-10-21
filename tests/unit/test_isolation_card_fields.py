"""審批卡的隔離欄位（iso-c2 U2h，設計 2.3 版 §2.8）：卡片文案要的事實由後端照實送.

- ``expected``：部署預期的層級（預期執行器時，開頭寫「執行器目前不能用」）；
- ``net``：降級去處那一層的網路設定問題（結尾寫擋不住、補救那行寫要加的覆寫設定）；
- ``block_ports_issue``：封鎖埠擋不全（讀不到設定或溢位）；
- ``local_issue``：讓降級只能「未隔離」的本機原因。
沒有值的欄位不送，事件和 C2-1 一樣精簡。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from src.services import approval_flow
from src.services.isolation.consent import ConsentKey, consent_ledger
from src.services.isolation.executor_client import ExecutorReady, ExecutorRoot
from src.services.isolation.status import IsolationSnapshot, LandlockState
from src.utils.isolation_policy import TCP_FEATURE, landlock_features

from tests.support.isolation import PinnedMonitor

if TYPE_CHECKING:
    from src.services.isolation.status import FallbackPlan
    from src.utils.isolation_policy import IsolationLevel

KEY = ConsentKey("Alice", "s1", "c1")
NO_TCP = sorted(landlock_features(6) - {TCP_FEATURE})


def _ready(net: str | None) -> ExecutorReady:
    roots = (ExecutorRoot("/data/personal_drive", "rw"),)
    return ExecutorReady(
        1, 6, True, net, ("fs", "tcp"), roots, "/x", "k" * 64, "s" * 64, "b" * 16, "p" * 64,
        1001,
    )


def _snapshot(
    level: IsolationLevel = "none",
    *,
    reason: str | None = None,
    local: LandlockState | None = None,
    executor_net: str | None = None,
    executor: bool = True,
    ports: str | None = None,
    local_issue: str | None = None,
) -> IsolationSnapshot:
    local = local or LandlockState("available", abi=6)
    return IsolationSnapshot(
        level, "executor", reason, None, local, local.features,
        local_issue=local_issue, executor=_ready(executor_net) if executor else None,
        block_ports_issue=ports,
    )


class _EligibleMonitor(PinnedMonitor):
    """這次在執行器範圍內（``executor_eligible``；接線屬 U2e）：執行器可用就是降級去處."""

    def fallback_for(
        self, *, profile_complete: bool, executor_eligible: bool = False
    ) -> FallbackPlan:
        del executor_eligible
        return super().fallback_for(profile_complete=profile_complete, executor_eligible=True)


async def _card(
    monkeypatch: pytest.MonkeyPatch, snap: IsolationSnapshot, *, reason: str | None,
    level: IsolationLevel = "none", executor_features: frozenset[str] | None = None,
) -> dict[str, object]:
    monitor = _EligibleMonitor(snap=snap, executor_features=executor_features)
    monkeypatch.setattr(approval_flow, "isolation_monitor", monitor)
    consent_ledger.record_policy(KEY, level=level, subject="x", reason=reason)
    card = await approval_flow.card_isolation(KEY, "execute_command")
    assert card is not None
    return card.event_field()


async def test_project_outside_the_executor_with_unsafe_local_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """綁專案＋網路設定不安全：去處是本機 Landlock，網路問題照那一層送出（結尾與補救要用）."""
    local = LandlockState("available", abi=6, net="net_mptcp")
    snap = _snapshot(local=local, executor_net="net_tcp_fastopen", reason="net_tcp_fastopen")
    field = await _card(monkeypatch, snap, reason="project_not_in_executor")
    assert field == {
        "level": "none", "fallback": "landlock", "features": NO_TCP,
        "reason": "project_not_in_executor", "reason_detail": None, "expected": "executor",
        "net": "net_mptcp",
    }


async def test_incomplete_block_ports_on_the_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """執行器可用、本機 Landlock 還在自測：去處是執行器，本機的原因不送（它沒有決定去處）."""
    snap = _snapshot(
        reason="block_ports_incomplete", ports="block_ports_incomplete",
        local=LandlockState("pending"), local_issue="probe_pending",
    )
    field = await _card(
        monkeypatch, snap, reason="block_ports_incomplete", executor_features=frozenset(NO_TCP),
    )
    assert field == {
        "level": "none", "fallback": "executor", "features": NO_TCP,
        "reason": "block_ports_incomplete", "reason_detail": None, "expected": "executor",
        "block_ports_issue": "block_ports_incomplete",
    }


async def test_executor_net_is_sent_when_the_executor_is_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _snapshot(reason="net_tcp_fastopen", executor_net="net_tcp_fastopen")
    field = await _card(
        monkeypatch, snap, reason="net_tcp_fastopen", executor_features=frozenset(NO_TCP),
    )
    assert (field["fallback"], field["net"]) == ("executor", "net_tcp_fastopen")
    assert "local_issue" not in field and "block_ports_issue" not in field


async def test_local_issue_is_sent_when_it_makes_the_fallback_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = LandlockState("unavailable", reason="landlock_unavailable")
    snap = _snapshot(
        reason="executor_unreachable", local=local, executor=False,
        local_issue="landlock_unavailable", ports="block_ports_incomplete",
    )
    field = await _card(monkeypatch, snap, reason="executor_unreachable")
    assert field == {
        "level": "none", "fallback": "direct", "features": [], "reason": "executor_unreachable",
        "reason_detail": None, "expected": "executor", "local_issue": "landlock_unavailable",
    }


async def test_an_isolated_card_carries_the_expected_level_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = replace(_snapshot("executor"), features=landlock_features(6))
    field = await _card(monkeypatch, snap, reason=None, level="executor")
    assert field == {
        "level": "executor", "features": sorted(landlock_features(6)), "reason": None,
        "reason_detail": None, "expected": "executor",
    }


async def test_an_unwired_tool_card_still_says_where_isolation_was_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approval_flow, "isolation_monitor", PinnedMonitor(snap=_snapshot()))
    monkeypatch.setattr(approval_flow, "ISOLATION_UNWIRED_TOOLS", frozenset({"unwired_probe"}))
    card = await approval_flow.card_isolation(KEY, "unwired_probe")
    assert card is not None
    assert card.event_field() == {
        "level": "none", "fallback": "direct", "features": [], "reason": "runner_not_wired",
        "reason_detail": None, "expected": "executor",
    }


async def test_creation_card_uses_recorded_executor_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        approval_flow, "isolation_monitor", PinnedMonitor(snap=_snapshot("executor")),
    )
    consent_ledger.record_policy(KEY, level="executor", subject="creation", executor_eligible=True)
    card = await approval_flow.card_isolation(KEY, "create_mcp")
    assert card.level == "executor"
    assert "fallback" not in card.event_field()


@pytest.mark.parametrize("tool,level", [("create_mcp", "executor"), ("scaffold_mcp", "landlock")])
async def test_creation_card_features_match_execution_scope(monkeypatch, tool, level):
    snap = replace(_snapshot("executor"), features=landlock_features(3))
    monitor = PinnedMonitor(snap=snap)
    monkeypatch.setattr(approval_flow, "isolation_monitor", monitor)
    consent_ledger.record_policy(KEY, level=level, subject="creation")
    card = await approval_flow.card_isolation(KEY, tool)
    assert card is not None and card.level == level
    expected = monitor.local_decision().features if tool == "scaffold_mcp" else snap.features
    assert expected != snap.features or tool == "create_mcp"
    assert card.event_field()["features"] == sorted(expected)
