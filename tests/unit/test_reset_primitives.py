"""軌B Batch 6 Phase 2a：ToukaProxy store scoped 清除原語——只清 DebugAgent、真實 agent（Jin）
不受影響、殘留歸零、每個 purge 都拒絕非 DebugAgent（零-API、沙盒）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from memory_server.tools import get_manager
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services.agent_reset import primitives as P
from src.services.feedback_store import FeedbackStore
from src.services.resource_manager import resource_manager
from src.utils import brain_persistence
from src.utils.async_db import get_system_setting, init_db_async, session_service
from src.utils.continuity_events_db import (
    EventRow,
    agent_epoch_key,
    count_agent_events,
    insert_event,
)

from tests.support.links import make_dir_link

DEBUG = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
JIN = AgentIdentity.for_role(agent_id="Jin", role="Owner")  # 代表真實 agent，必須不受影響


async def test_sessions_isolation():
    await init_db_async()  # 沙盒 DB 需先建表（user_sessions）
    await session_service.upsert_session("s_debug", "t", [], identity=DEBUG)
    await session_service.upsert_session("s_jin", "t", [], identity=JIN)
    assert await P.purge_sessions("DebugAgent") >= 1
    assert await P.remaining_sessions("DebugAgent") == 0
    assert await session_service.count_agent_sessions(JIN) >= 1


async def test_feedback_isolation():
    await FeedbackStore.submit("s1", "m1", DEBUG, "up")
    await FeedbackStore.submit("s2", "m1", JIN, "up")
    assert await P.purge_feedback("DebugAgent") >= 1
    assert await P.remaining_feedback("DebugAgent") == 0
    assert await FeedbackStore.count_agent(JIN) >= 1


async def test_brain_isolation():
    brain_persistence.set_active_brain(DEBUG, "model-x")
    brain_persistence.set_active_brain(JIN, "model-y")
    assert await P.purge_brain("DebugAgent") == 1
    assert await P.remaining_brain("DebugAgent") == 0
    assert brain_persistence.has_agent(JIN) is True


async def test_stats_isolation():
    for ident in (DEBUG, JIN):
        d = Path(resource_manager.get_stats_path(ident))
        d.mkdir(parents=True, exist_ok=True)
        (d / "token_usage.json").write_text("{}", encoding="utf-8")
    assert await P.purge_stats("DebugAgent") == 1
    assert await P.remaining_stats("DebugAgent") == 0
    assert Path(resource_manager.get_stats_path(JIN)).exists()


async def test_outputs_isolation():
    for ident in (DEBUG, JIN):
        report = resource_manager.outputs_dir(ident).child("report.xlsx")
        resource_manager.zone_write_text(report, "x")
    await P.purge_outputs("DebugAgent")
    assert await P.remaining_outputs("DebugAgent") == 0
    jin_report = resource_manager.outputs_dir(JIN).child("report.xlsx")
    assert resource_manager.zone_kind(jin_report) == "file"  # 真實 agent 的產出不受影響


async def test_outputs_purge_preserves_user_uploads():
    """E65（ADR-0175）：outputs/uploads 是使用者上傳檔（非 agent 產出）——purge 不刪、
    remaining 不計（否則自檢 false-nonclean）；其餘 outputs 內容照清。"""
    outputs, uploads = resource_manager.outputs_dir(DEBUG), resource_manager.uploads_dir(DEBUG)
    resource_manager.zone_write_text(outputs.child("report.xlsx"), "x")
    resource_manager.zone_write_text(uploads.child("photo.png"), "img")

    assert await P.purge_outputs("DebugAgent") == 1  # 只計 agent 產出（uploads 不入帳）
    assert await P.remaining_outputs("DebugAgent") == 0  # uploads 不算殘留
    assert resource_manager.zone_read_text(uploads.child("photo.png")) == "img"  # 使用者上傳存活
    assert resource_manager.zone_kind(outputs.child("report.xlsx")) == "missing"


async def test_a_link_inside_outputs_is_removed_and_uploads_kept(zone_data_root, tmp_path):
    """ADR-0293：outputs 裡的連結只刪本身、不進入；uploads 照舊保留。"""
    fake = tmp_path.resolve() / "fake_config"
    fake.mkdir()
    (fake / "users.json").write_text("{}", encoding="utf-8")
    outputs = zone_data_root / "custom" / "DebugAgent" / "drive" / "outputs"
    (outputs / "uploads").mkdir(parents=True)
    (outputs / "uploads" / "photo.png").write_text("img", encoding="utf-8")
    (outputs / "report.xlsx").write_text("x", encoding="utf-8")
    make_dir_link(outputs / "linked", fake)

    assert await P.purge_outputs("DebugAgent") == 2  # report.xlsx 與連結本身
    assert await P.remaining_outputs("DebugAgent") == 0
    assert sorted(child.name for child in outputs.iterdir()) == ["uploads"]
    assert (outputs / "uploads" / "photo.png").read_text(encoding="utf-8") == "img"
    assert (fake / "users.json").read_text(encoding="utf-8") == "{}"


async def test_memory_isolation():
    fs = get_manager().get_factual_store()
    fs.save_fact(DEBUG, "debug fact about 燈火")
    fs.save_fact(JIN, "jin private fact")
    assert fs.count_agent_facts(DEBUG) >= 1
    await P.purge_memory("DebugAgent")
    assert await P.remaining_memory("DebugAgent") == 0  # facts + vectors 皆 0
    assert fs.count_agent_facts(JIN) >= 1  # 真實 agent 記憶不受影響


def _mission_file(ident: AgentIdentity):
    return resource_manager.agent_drive(ident).child("missions", "current_mission.json")


async def test_mission_isolation():
    for ident in (DEBUG, JIN):
        resource_manager.zone_write_text(_mission_file(ident), "{}")
    await P.purge_mission("DebugAgent")
    assert await P.remaining_mission("DebugAgent") == 0
    assert resource_manager.zone_kind(_mission_file(JIN)) == "file"


async def test_snapshot_isolation():
    base = Path(resource_manager.get_base_path()) / "snapshots"
    base.mkdir(parents=True, exist_ok=True)
    (base / "DebugAgent.md").write_text("debug snapshot", encoding="utf-8")
    (base / "Jin.md").write_text("jin snapshot", encoding="utf-8")
    assert await P.purge_snapshot("DebugAgent") == 1
    assert await P.remaining_snapshot("DebugAgent") == 0
    assert (base / "Jin.md").exists()  # 真實 agent 的快照不受影響


async def test_timeline_isolation():
    """活動事件（屬「記憶」類別）：只清 DebugAgent 的，真實 agent 不受影響，並寫重置起點。"""
    await init_db_async()
    for ident, subject in ((DEBUG, "f-debug"), (JIN, "f-jin")):
        await insert_event(
            EventRow(
                event_id=f"memory_saved:{ident.agent_id}:{subject}",
                agent=ident.agent_id,
                kind="memory_saved",
                occurred_at=1,
                subject=subject,
            )
        )
    jin_before = await count_agent_events(JIN)
    assert await P.purge_timeline("DebugAgent") >= 1
    assert await P.remaining_timeline("DebugAgent") == 0
    assert await count_agent_events(JIN) == jin_before >= 1
    assert await get_system_setting(agent_epoch_key(DEBUG)) is not None
    assert [s.name for s in P.MEMORY_STORES][:2] == ["memory", "timeline"]


async def test_memory_unavailable_raises(monkeypatch):
    """記憶橋接不可用時 purge/remaining 必須 raise（禁 false-clean，ADR-0104 R11）。"""
    def _boom() -> object:
        raise ToukaException(ErrorCode.SYSTEM_ERROR)

    monkeypatch.setattr(P, "_memory_manager", _boom)
    with pytest.raises(ToukaException):
        await P.purge_memory("DebugAgent")
    with pytest.raises(ToukaException):
        await P.remaining_memory("DebugAgent")


@pytest.mark.parametrize("store", [s.name for s in P.ALL_STORES])
async def test_every_purge_rejects_non_debugagent(store):
    sp = next(s for s in P.ALL_STORES if s.name == store)
    with pytest.raises(ToukaException):
        await sp.purge("Jin")
