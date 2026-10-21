"""FeedbackStore 單元測試（HAX G15/G6，ADR-0084）。

驗證隔離儲存的 upsert／撤回／依身分過濾／原因截斷，以及「僅碰 feedback.db、
不接觸 soul/legacy/記憶系統」的安全邊界。每測試用唯一 session_id，對共享 sandbox DB 免疫。
身分以 AgentIdentity 物件傳入 (Identity SSOT)。
"""

import pytest
from src.models.identity import AgentIdentity
from src.services.feedback_store import FeedbackStore, get_feedback_db_path

DEBUG = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
DISPATCH = AgentIdentity.for_role(agent_id="DispatchAgent", role="Owner", capabilities=[])


@pytest.mark.asyncio
async def test_submit_and_get_session_map():
    fid = await FeedbackStore.submit("s_submit", "m1", DEBUG, "up")
    assert fid
    mapping = await FeedbackStore.get_session_map(DEBUG, "s_submit")
    assert mapping["m1"]["type"] == "up"


@pytest.mark.asyncio
async def test_upsert_overwrites_same_key():
    await FeedbackStore.submit("s_upsert", "m1", DEBUG, "up")
    await FeedbackStore.submit("s_upsert", "m1", DEBUG, "down", reason="不準確")
    mapping = await FeedbackStore.get_session_map(DEBUG, "s_upsert")
    assert mapping["m1"]["type"] == "down"
    assert mapping["m1"]["reason"] == "不準確"
    # 同鍵僅留一筆（UNIQUE 約束）
    recent = await FeedbackStore.get_recent(500)
    same = [r for r in recent if r["session_id"] == "s_upsert"]
    assert len(same) == 1


@pytest.mark.asyncio
async def test_none_retracts_row():
    await FeedbackStore.submit("s_retract", "m1", DEBUG, "up")
    result = await FeedbackStore.submit("s_retract", "m1", DEBUG, "none")
    assert result is None
    mapping = await FeedbackStore.get_session_map(DEBUG, "s_retract")
    assert "m1" not in mapping


@pytest.mark.asyncio
async def test_session_map_filters_by_agent():
    await FeedbackStore.submit("s_agent", "m1", DEBUG, "up")
    await FeedbackStore.submit("s_agent", "m2", DISPATCH, "down")
    mapping = await FeedbackStore.get_session_map(DEBUG, "s_agent")
    assert "m1" in mapping
    assert "m2" not in mapping


@pytest.mark.asyncio
async def test_reason_capped_at_500():
    await FeedbackStore.submit("s_cap", "m1", DEBUG, "down", reason="x" * 999)
    mapping = await FeedbackStore.get_session_map(DEBUG, "s_cap")
    assert len(mapping["m1"]["reason"]) == 500


@pytest.mark.asyncio
async def test_recent_sorted_newest_first():
    await FeedbackStore.submit("s_recent_a", "m1", DEBUG, "up")
    await FeedbackStore.submit("s_recent_b", "m1", DEBUG, "down")
    recent = await FeedbackStore.get_recent(500)
    mine = [r for r in recent if r["session_id"].startswith("s_recent_")]
    # 最新（s_recent_b）排在前
    assert mine[0]["session_id"] == "s_recent_b"


@pytest.mark.asyncio
async def test_store_is_isolated_from_soul_and_legacy():
    await FeedbackStore.submit("s_iso", "m1", DEBUG, "up")
    path = str(get_feedback_db_path())
    assert path.endswith("feedback.db")
    # 安全邊界：永不落在 soul/legacy 受保護區
    lowered = path.replace("\\", "/").lower()
    assert "/soul/" not in lowered
    assert "/legacy/" not in lowered
