"""軌B Batch 6 Phase 3：重置 orchestrator——備份優先、清完報告完整性、備份失敗即中止、
保留身分、拒絕非 DebugAgent（零-API；mock create_backup 避免真實備份）。
"""
from __future__ import annotations

import pytest

from memory_server.tools import get_manager
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services import system_backup
from src.services.agent_reset import orchestrator as O
from src.services.agent_reset import primitives as P
from src.utils import brain_persistence
from src.utils.async_db import init_db_async

DEBUG = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
JIN = AgentIdentity.for_role(agent_id="Jin", role="Owner")
_FAKE_BACKUP = {"status": "success", "message": "ok", "output": "backups/x.zip"}


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
