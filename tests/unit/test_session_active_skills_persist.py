"""ADR-0062：user_sessions.active_skills 後端持久化測試（TDD）。

驗證 active_skills 的完整持久化鏈路：
  1. upsert_session() 接受並儲存 active_skills
  2. get_session_by_id() 返回 active_skills（JSON parse）
  3. get_all_sessions() 列表包含 active_skills
  4. SessionModel 接受 active_skills 欄位（Pydantic schema）
  5. merge 語意：不傳 active_skills 時保留現有值（同 active_mcp_servers）
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from src.routers.ui_sessions import SessionModel
from src.utils.async_db import SessionService, init_db_async


def _sess_id() -> str:
    return f"test-skills-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_upsert_with_active_skills_persists_and_returns(tmp_path):
    """upsert 帶 active_skills → get_session_by_id 返回正確值。"""
    db_path = tmp_path / "test.db"
    sid = _sess_id()

    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        success = await SessionService.upsert_session(
            session_id=sid,
            title="T",
            messages={"messages": {}, "currentId": None},
            active_skills=["skill-x"],
        )
        assert success is True

        session = await SessionService.get_session_by_id(sid)

    assert session is not None
    assert session["active_skills"] == ["skill-x"]


@pytest.mark.asyncio
async def test_upsert_without_active_skills_defaults_to_empty_list(tmp_path):
    """不傳 active_skills 參數 → 返回 []（非 None 或 KeyError）。"""
    db_path = tmp_path / "test.db"
    sid = _sess_id()

    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        await SessionService.upsert_session(
            session_id=sid,
            title="T",
            messages={"messages": {}, "currentId": None},
        )
        session = await SessionService.get_session_by_id(sid)

    assert session is not None
    assert session.get("active_skills") == []


@pytest.mark.asyncio
async def test_upsert_preserves_active_skills_on_partial_update(tmp_path):
    """第二次 upsert 不傳 active_skills → 保留第一次設定的值（merge 語意）。"""
    db_path = tmp_path / "test.db"
    sid = _sess_id()

    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        await SessionService.upsert_session(
            session_id=sid,
            title="T",
            messages={"messages": {}, "currentId": None},
            active_skills=["skill-a"],
        )
        # 第二次 upsert，不傳 active_skills
        await SessionService.upsert_session(
            session_id=sid,
            title="T updated",
            messages={"messages": {}, "currentId": None},
        )
        session = await SessionService.get_session_by_id(sid)

    assert session is not None
    assert session["active_skills"] == ["skill-a"]


@pytest.mark.asyncio
async def test_upsert_can_clear_active_skills_with_empty_list(tmp_path):
    """active_skills=[] 明確清空 → 返回 []。"""
    db_path = tmp_path / "test.db"
    sid = _sess_id()

    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        await SessionService.upsert_session(
            session_id=sid,
            title="T",
            messages={"messages": {}, "currentId": None},
            active_skills=["skill-a"],
        )
        await SessionService.upsert_session(
            session_id=sid,
            title="T",
            messages={"messages": {}, "currentId": None},
            active_skills=[],
        )
        session = await SessionService.get_session_by_id(sid)

    assert session is not None
    assert session["active_skills"] == []


@pytest.mark.asyncio
async def test_list_sessions_includes_active_skills(tmp_path):
    """get_all_sessions() 列表中的 session 包含 active_skills。"""
    db_path = tmp_path / "test.db"
    sid = _sess_id()

    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        await SessionService.upsert_session(
            session_id=sid,
            title="Listed Session",
            messages={"messages": {}, "currentId": None},
            active_skills=["skill-x"],
        )
        sessions = await SessionService.get_all_sessions()

    matched = [s for s in sessions if s["id"] == sid]
    assert len(matched) == 1, f"Session {sid} not found in list"
    assert matched[0]["active_skills"] == ["skill-x"]


def test_session_model_accepts_and_validates_active_skills():
    """SessionModel 接受 active_skills 欄位；預設值為 []。"""
    m = SessionModel(
        id="test-001",
        title="T",
        messages=[],
        updatedAt=1234567890,
        active_skills=["skill-a"],
    )
    assert m.active_skills == ["skill-a"]

    m_default = SessionModel(
        id="test-002",
        title="T",
        messages=[],
        updatedAt=1234567890,
    )
    assert m_default.active_skills == []
