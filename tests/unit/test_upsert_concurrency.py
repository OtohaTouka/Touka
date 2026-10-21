"""P5d：upsert_session 併發鎖 — 同一會話併發寫入不互相覆蓋丟欄位。

upsert_session 是 read(existing)-modify-write；兩個併發 upsert 各設不同欄位、其餘
靠 existing merge 時，若無鎖會讀到 stale existing 而把對方剛寫的欄位蓋回舊值。
本測試驗證 _get_session_lock 串行化後兩欄位皆存活（比照 append_message_to_session）。
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

from src.utils.async_db import SessionService, init_db_async


def _sess_id() -> str:
    return f"test-concurrency-{uuid.uuid4().hex[:8]}"


async def test_concurrent_upserts_preserve_distinct_fields(tmp_path):
    """併發設 active_skills 與 project_id（title 固定）→ 兩者皆存活。"""
    db_path = tmp_path / "test.db"
    sid = _sess_id()
    msgs = {"messages": {}, "currentId": None}

    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        # 初始狀態：skills=[]、project=P0
        await SessionService.upsert_session(
            session_id=sid, title="T0", messages=msgs,
            active_skills=[], project_id="P0",
        )
        # 兩路併發：A 只設 skills、B 只設 project（其餘 None → 由 existing merge）
        await asyncio.gather(
            SessionService.upsert_session(
                session_id=sid, title="T0", messages=msgs, active_skills=["S2"]
            ),
            SessionService.upsert_session(
                session_id=sid, title="T0", messages=msgs, project_id="P3"
            ),
        )
        session = await SessionService.get_session_by_id(sid)

    assert session is not None
    # 串行化後兩欄位皆未被 stale 覆蓋
    assert session["active_skills"] == ["S2"]
    assert session["project_id"] == "P3"
