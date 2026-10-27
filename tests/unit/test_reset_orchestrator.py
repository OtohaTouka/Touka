"""軌B Batch 6 Phase 3：重置 orchestrator——備份優先、清完報告完整性、備份失敗即中止、
保留身分、拒絕非 DebugAgent（零-API；mock create_backup 避免真實備份）。
"""
from __future__ import annotations

import pytest

from memory_server.tools import get_manager
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services import destructive_ops, system_backup
from src.services.agent_reset import orchestrator as O
from src.services.agent_reset import primitives as P
from src.services.rhythm_service import rhythm_service
from src.utils import brain_persistence
from src.utils.async_db import init_db_async

DEBUG = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
JIN = AgentIdentity.for_role(agent_id="Jin", role="Owner")
_FAKE_BACKUP = {"status": "success", "message": "ok", "output": "backups/x.zip"}


def _who(name: str) -> AgentIdentity:
    return AgentIdentity(agent_id=name)


async def test_reset_backup_first_then_purges_clean(monkeypatch):
    await init_db_async()
    calls: list[str] = []
    monkeypatch.setattr(system_backup, "create_backup", lambda: (calls.append("backup"), _FAKE_BACKUP)[1])

    fs = get_manager().get_factual_store()
    fs.save_fact(DEBUG, "debug fact")
    fs.save_fact(JIN, "jin fact")
    brain_persistence.set_active_brain(DEBUG, "m")
    brain_persistence.set_active_brain(JIN, "m")

    report = await O.reset_debug_agent_data()

    assert calls == ["backup"]  # 備份在清除之前被呼叫
    assert report.backup == _FAKE_BACKUP
    assert report.clean is True  # 全 store 殘留 0、無錯誤
    # DebugAgent 清乾淨
    assert fs.count_agent_facts(DEBUG) == 0
    assert brain_persistence.has_agent(DEBUG) is False
    # 真實 agent（Jin）完全不受影響
    assert fs.count_agent_facts(JIN) >= 1
    assert brain_persistence.has_agent(JIN) is True


async def test_reset_aborts_when_backup_fails(monkeypatch):
    def _boom() -> dict:
        raise ToukaException(ErrorCode.SYSTEM_BACKUP_FAILED)

    monkeypatch.setattr(system_backup, "create_backup", _boom)
    fs = get_manager().get_factual_store()
    fs.save_fact(DEBUG, "must survive: backup failed → no delete")

    with pytest.raises(ToukaException):
        await O.reset_debug_agent_data()

    assert fs.count_agent_facts(DEBUG) >= 1  # 備份失敗 → 一筆都沒刪


async def test_reset_skips_backup_when_disabled(monkeypatch):
    def _should_not_run() -> dict:
        raise AssertionError("backup must not be called when do_backup=False")

    monkeypatch.setattr(system_backup, "create_backup", _should_not_run)
    report = await O.reset_debug_agent_data(do_backup=False)
    assert report.backup is None


async def test_reset_rejects_non_debugagent(monkeypatch):
    monkeypatch.setattr(system_backup, "create_backup", lambda: _FAKE_BACKUP)
    with pytest.raises(ToukaException):
        await O.reset_debug_agent_data("Jin")


def test_registry_never_touches_identity_or_registration():
    names = {s.name for s in P.ALL_STORES}
    assert not (names & {"identity", "identity_map", "users", "registry", "agents_registry"})


async def test_memory_unavailable_forces_not_clean(monkeypatch):
    """ADR-0104 R11：記憶橋接不可用 → 不可 false-clean；memory store 記錯、clean=False。"""
    await init_db_async()
    monkeypatch.setattr(system_backup, "create_backup", lambda: _FAKE_BACKUP)

    def _boom() -> object:
        raise ToukaException(ErrorCode.SYSTEM_ERROR)

    monkeypatch.setattr(P, "_memory_manager", _boom)
    report = await O.reset_debug_agent_data()

    assert report.clean is False
    mem = next(s for s in report.stores if s.name == "memory")
    assert mem.error  # 記憶無法確認 → 記錄錯誤（而非靜默回 0）


async def test_concurrent_reset_is_rejected(monkeypatch):
    """ADR-0104 R7：已有 reset 進行中時，第二個 reset 立即被拒（序列化鎖）。"""
    monkeypatch.setattr(system_backup, "create_backup", lambda: _FAKE_BACKUP)
    async with O._reset_lock:  # 模擬「進行中」
        with pytest.raises(ToukaException):
            await O.reset_debug_agent_data(do_backup=False)


def test_to_dict_lists_retained_audit_stores():
    """報告須透明列出刻意保留（未清）的稽核 store，clean=True 才不被誤讀（ADR-0104 R3/R4/R12）。"""
    report = O.ResetReport(agent="DebugAgent", clean=True)
    names = {r["name"] for r in report.to_dict()["retained"]}
    assert {"legacy", "project_audit", "decisions.csv"} <= names


class _HeldProbe:
    """假的 store：清除時記下目標是否被標記（心跳看到就停）。"""

    name = "probe"

    def __init__(self) -> None:
        self.seen: list[tuple[bool, bool]] = []

    async def purge(self, normalized: str, _guard: object) -> int:
        held = destructive_ops.is_held(_who(normalized))
        self.seen.append((held, rhythm_service.is_interrupted("hb-reset")))
        return 0

    async def remaining(self, _normalized: str, _guard: object) -> int:
        return 0


async def test_reset_holds_its_target_and_stops_its_heartbeats(monkeypatch):
    """舊審查報告丙-4：重置期間標記目標，並叫這個靈魂正在跑的心跳停下（別的靈魂不動）。"""
    probe = _HeldProbe()
    monkeypatch.setattr(O, "ALL_STORES", [probe])
    try:
        assert destructive_ops.start_heartbeat(_who("DebugAgent"), "hb-reset") is not None
        assert destructive_ops.start_heartbeat(_who("Jin"), "hb-jin") is not None
        report = await O.reset_debug_agent_data(do_backup=False)
        stopped = rhythm_service.is_interrupted("hb-reset")
        other = rhythm_service.is_interrupted("hb-jin")
    finally:
        for agent, sid in (("DebugAgent", "hb-reset"), ("Jin", "hb-jin")):
            destructive_ops.end_heartbeat(_who(agent), sid)
            rhythm_service.clear_interrupt(sid)
    assert report.clean is True
    assert probe.seen == [(True, True)]  # 清除時已經標記、已經叫停
    assert (stopped, other) == (True, False)
    assert not destructive_ops.is_held(_who("DebugAgent"))


async def test_a_failed_reset_releases_its_target(monkeypatch):
    def _boom() -> dict:
        raise ToukaException(ErrorCode.SYSTEM_BACKUP_FAILED)

    monkeypatch.setattr(system_backup, "create_backup", _boom)
    monkeypatch.setattr(O, "ALL_STORES", [_HeldProbe()])
    with pytest.raises(ToukaException):
        await O.reset_debug_agent_data()
    assert not destructive_ops.is_held(_who("DebugAgent"))
    assert not O.reset_in_progress()


async def test_a_refused_reset_does_not_stop_heartbeats(monkeypatch):
    """勾了清除、預檢沒過：什麼都沒做就回錯誤，也不叫停這個靈魂的心跳。"""

    async def refuse(*_args: object) -> None:
        raise ToukaException(ErrorCode.MEMORY_SCRUB_UNAVAILABLE)

    monkeypatch.setattr(O, "_prepare_scrub", refuse)
    try:
        before = destructive_ops.start_heartbeat(_who("Jin"), "hb-keep")
        assert before is not None
        with pytest.raises(ToukaException):
            await O._execute_reset("Jin", lambda c: c, do_backup=False, scrub=True, actor_label="x")
        stopped = rhythm_service.is_interrupted("hb-keep")
        disturbed = destructive_ops.disturbed(_who("Jin"), before)
    finally:
        destructive_ops.end_heartbeat(_who("Jin"), "hb-keep")
        rhythm_service.clear_interrupt("hb-keep")
    assert stopped is False
    assert disturbed is False  # 下一個脈衝前也不會停
    assert not O.reset_in_progress()
