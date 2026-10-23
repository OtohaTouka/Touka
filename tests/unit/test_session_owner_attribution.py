"""Session 明確人類歸屬（owner_user_id）——TDD。

計畫：docs/architecture/proposals/session-owner-attribution-plan.md（比照 ADR-0107
對 projects 的做法；ADR-0131 明列的授權地基長期正解）。

覆蓋：
  1. 遷移：owner_user_id 欄位存在、重複 init 冪等（沿用 projects pattern）
  2. attribution：INSERT 記 owner；UPDATE 永不改（後寫者/agent 皆不得蓋走）
  3. 污染回歸鎖：_persist_assistant_turn（human 呼叫者）不再把 human UUID 寫進 agent_id
  4. 授權矩陣：owner 聯集通道（additive）；NULL legacy 行為逐位不變
  5. 指派檢查：裸模型 id 放行（顯示標籤）、真 agent id 維持 is_owner 防護
  6. 出口消毒：list/get 回應不含 owner_user_id（internal authz field）
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.ui_sessions import _can_access_session, _is_registered_agent
from src.services.dispatcher import _persist_assistant_turn
from src.services.identity_router import get_verified_identity
from src.utils.async_db import SessionService, get_async_db, init_db_async

HUMAN_OWNER = AgentIdentity.for_role(agent_id="user-uuid-1", role="normal", is_human=True)
HUMAN_OTHER = AgentIdentity.for_role(agent_id="user-uuid-2", role="normal", is_human=True)
ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True)
DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", is_human=False)
AGENT = AgentIdentity.for_role(agent_id="Alice", role="Owner", is_human=False)

EMPTY_TREE = {"messages": {}, "currentId": None}


def _sid() -> str:
    return f"test-owner-{uuid.uuid4().hex[:8]}"


# ---- 1. 遷移 ----


@pytest.mark.asyncio
async def test_migration_owner_column_exists_and_idempotent(tmp_path):
    """owner_user_id 欄位存在於 user_sessions；重複 init 冪等不炸。"""
    db_path = tmp_path / "test.db"
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        await init_db_async()  # 冪等

        async with get_async_db() as db:
            cursor = await db.execute("PRAGMA table_info(user_sessions)")
            columns = [row[1] for row in await cursor.fetchall()]
    assert "owner_user_id" in columns


# ---- 2. attribution：INSERT-only ----


@pytest.mark.asyncio
async def test_insert_records_owner_and_service_layer_exposes_it(tmp_path):
    """INSERT 帶 owner_user_id → service 層（get_session_by_id）透出該欄位。"""
    db_path = tmp_path / "test.db"
    sid = _sid()
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        ok = await SessionService.upsert_session(
            session_id=sid,
            title="T",
            messages=EMPTY_TREE,
            owner_user_id="user-uuid-1",
        )
        assert ok is True
        session = await SessionService.get_session_by_id(sid)
    assert session is not None
    assert session["owner_user_id"] == "user-uuid-1"


@pytest.mark.asyncio
async def test_insert_without_owner_stays_null_legacy(tmp_path):
    """不傳 owner → NULL（legacy 語意）。"""
    db_path = tmp_path / "test.db"
    sid = _sid()
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        await SessionService.upsert_session(session_id=sid, title="T", messages=EMPTY_TREE)
        session = await SessionService.get_session_by_id(sid)
    assert session is not None
    assert session.get("owner_user_id") is None


@pytest.mark.asyncio
async def test_update_never_changes_owner(tmp_path):
    """UPDATE 永不改 owner：他人 human 蓋寫不走、agent 不觸碰、None 不清空。"""
    db_path = tmp_path / "test.db"
    sid = _sid()
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        await SessionService.upsert_session(
            session_id=sid,
            title="T",
            messages=EMPTY_TREE,
            identity=AGENT,
            owner_user_id="user-uuid-1",
        )
        # 他人 human 嘗試蓋走
        await SessionService.upsert_session(
            session_id=sid,
            title="T2",
            messages=EMPTY_TREE,
            identity=AGENT,
            owner_user_id="user-uuid-2",
        )
        # agent 更新（不傳 owner）
        await SessionService.upsert_session(
            session_id=sid,
            title="T3",
            messages=EMPTY_TREE,
            identity=AGENT,
        )
        session = await SessionService.get_session_by_id(sid)
    assert session is not None
    assert session["owner_user_id"] == "user-uuid-1"
    assert session["title"] == "T3"  # 其他欄位照常更新


# ---- 3. 污染回歸鎖（D3）----


@pytest.mark.asyncio
async def test_persist_assistant_turn_human_does_not_pollute_agent_id(tmp_path):
    """human 呼叫者的伺服器端持久化：agent_id 保留原模型 id、owner 記錄 human id。

    ADR-0131 調查實證：舊行為把呼叫者 id 直接當 agent_id upsert（`agent_id or
    existing` 新值蓋舊）→ human UUID／DebugAgent 污染進 agent_id（`data/custom/<uuid>/`
    目錄即遺跡）。
    """
    db_path = tmp_path / "test.db"
    sid = _sid()
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        # 模擬前端 syncSession 建立的裸模型對話（legacy：無 owner）
        await SessionService.upsert_session(
            session_id=sid,
            title="T",
            messages=EMPTY_TREE,
            identity=AgentIdentity(agent_id="gemini-2.5-flash"),
        )
        await _persist_assistant_turn(
            sid, "hello reply", "gemini-2.5-flash", HUMAN_OWNER, check_access=False
        )
        session = await SessionService.get_session_by_id(sid)
    assert session is not None
    assert session["agent_id"] == "gemini-2.5-flash"  # 不被 human UUID 污染
    # 不 runtime claim：owner 僅 INSERT 記錄；既有 legacy session 走
    # UPDATE 路徑 → owner 維持 NULL（真實流程中 owner 由 save_session 建立時記錄）。
    assert session.get("owner_user_id") is None


@pytest.mark.asyncio
async def test_persist_assistant_turn_agent_keeps_current_behavior(tmp_path):
    """agent 呼叫者（heartbeat 型）：agent_id 沿用現行為、owner 不觸碰。"""
    db_path = tmp_path / "test.db"
    sid = _sid()
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        await SessionService.upsert_session(
            session_id=sid,
            title="T",
            messages=EMPTY_TREE,
            identity=AGENT,
            owner_user_id="user-uuid-1",
        )
        await _persist_assistant_turn(sid, "pulse reply", "Alice", AGENT, check_access=False)
        session = await SessionService.get_session_by_id(sid)
    assert session is not None
    assert session["agent_id"] == "Alice"  # 現行為：agent 身分照寫
    assert session["owner_user_id"] == "user-uuid-1"  # owner 不被觸碰


# ---- 4. 授權矩陣（D4 聯集通道）----


def test_can_access_session_owner_union_matrix():
    """owner 聯集通道：新增 human-owner 通道、既有語意逐位不變。"""
    model_id = "gemini-2.5-flash"
    with patch("src.services.resource_manager.resource_manager.is_owner", return_value=False):
        # 新通道：owner 本人（normal human）可存取
        assert _can_access_session(HUMAN_OWNER, model_id, "user-uuid-1") is True
        # 他人 normal：非 owner 也非 agent-owner → 不可見
        assert _can_access_session(HUMAN_OTHER, model_id, "user-uuid-1") is False
        # 非 human 呼叫者不得走 owner 通道（is_human 必要）
        assert (
            _can_access_session(
                AgentIdentity.for_role(agent_id="user-uuid-1", role="Base", is_human=False),
                model_id,
                "user-uuid-1",
            )
            is False
        )
        # legacy NULL owner：行為不變（normal → False）
        assert _can_access_session(HUMAN_OWNER, model_id, None) is False
        assert _can_access_session(HUMAN_OWNER, None, None) is False
    # admin / Debugger：全域不變（不依賴 is_owner）
    assert _can_access_session(ADMIN, model_id, None) is True
    assert _can_access_session(DEBUGGER, model_id, None) is True
    # owner 有值但 agent-derived 通道成立時仍可過（聯集、零縮窄）
    with patch("src.services.resource_manager.resource_manager.is_owner", return_value=True):
        assert _can_access_session(HUMAN_OTHER, "Alice", "user-uuid-1") is True


# ---- 5. 指派檢查（D5）----


def test_is_registered_agent_helper():
    """名冊判定：真 agent → True；裸模型 id → False；名冊不可用 → fail-closed True。"""
    fake_agents = [{"id": "Alice"}, {"id": "ToukaGroup"}]
    with patch("src.routers.ui_sessions.agent_service.get_all_agents", return_value=fake_agents):
        assert _is_registered_agent("Alice") is True
        assert _is_registered_agent("gemini-2.5-flash") is False
    with patch(
        "src.routers.ui_sessions.agent_service.get_all_agents",
        side_effect=RuntimeError("registry down"),
    ):
        assert _is_registered_agent("anything") is True  # fail-closed：維持嚴格防護


@pytest.fixture
def normal_client(tmp_path):
    """normal human 身分的 TestClient，DB 走 tmp_path 沙盒。"""
    db_path = tmp_path / "route.db"
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        app.dependency_overrides[get_verified_identity] = lambda: HUMAN_OWNER
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()


def _session_payload(client: TestClient, sid: str, agent_id: str | None) -> dict:
    snapshot = client.get(f"/v1/ui/sessions/{sid}?snapshot=true")
    assert snapshot.status_code == 200
    assert snapshot.json()["exists"] is False
    body = {
        "id": sid,
        "title": "T",
        "messages": [],
        "updatedAt": 1751700000000,
        "revision": snapshot.json()["revision"],
    }
    if agent_id:
        body["agent_id"] = agent_id
    return body


def test_save_session_normal_user_bare_model_id_allowed(normal_client):
    """normal 使用者把對話標成裸模型 id → 放行落盤（過去 403＝ui_sessions.db 長年空白主因之一）。"""
    sid = _sid()
    fake_agents = [{"id": "Alice"}]
    with patch("src.routers.ui_sessions.agent_service.get_all_agents", return_value=fake_agents):
        res = normal_client.post(
            f"/v1/ui/sessions/{sid}", json=_session_payload(normal_client, sid, "gemini-2.5-flash")
        )
    assert res.status_code == 200, res.text

    # 落盤且 owner 歸屬本人 → 本人 list 可見（端到端反面驗證）
    res_list = normal_client.get("/v1/ui/sessions")
    assert res_list.status_code == 200
    ids = [s["id"] for s in res_list.json()]
    assert sid in ids


def test_save_session_normal_user_real_agent_still_denied(normal_client):
    """normal 使用者把對話指派給他人真 agent → 維持 403（is_owner 防護不弱化）。"""
    sid = _sid()
    fake_agents = [{"id": "Alice"}]
    with (
        patch("src.routers.ui_sessions.agent_service.get_all_agents", return_value=fake_agents),
        patch("src.services.resource_manager.resource_manager.is_owner", return_value=False),
    ):
        res = normal_client.post(
            f"/v1/ui/sessions/{sid}", json=_session_payload(normal_client, sid, "Alice")
        )
    assert res.status_code == 403


# ---- 6. 出口消毒（D6）----


def test_list_and_get_strip_owner_user_id(normal_client):
    """list/get 回應不得含 owner_user_id（internal authz field，比照 projects）。"""
    sid = _sid()
    fake_agents: list[dict] = []
    with patch("src.routers.ui_sessions.agent_service.get_all_agents", return_value=fake_agents):
        res = normal_client.post(
            f"/v1/ui/sessions/{sid}", json=_session_payload(normal_client, sid, "gemini-2.5-flash")
        )
        assert res.status_code == 200, res.text

    listed = normal_client.get("/v1/ui/sessions").json()
    mine = [s for s in listed if s["id"] == sid]
    assert len(mine) == 1
    assert "owner_user_id" not in mine[0]

    got = normal_client.get(f"/v1/ui/sessions/{sid}")
    assert got.status_code == 200
    assert "owner_user_id" not in got.json()


# ---- Phase 5：backfill 運維腳本（dry-run 預設、只填 NULL、冪等）----


def _load_backfill_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "backfill_session_owner.py"
    spec = importlib.util.spec_from_file_location("backfill_session_owner", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_session_owner"] = mod  # 先註冊再 exec（guard 自測先例）
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing_and_apply_is_idempotent(tmp_path):
    """dry-run 只統計不寫；apply 只填 NULL、不碰既有歸屬；重跑冪等。"""
    db_path = tmp_path / "bf.db"
    mod = _load_backfill_module()
    with patch("src.utils.async_db.get_db_path", return_value=db_path):
        await init_db_async()
        await SessionService.upsert_session(session_id="s-null", title="T", messages=EMPTY_TREE)
        await SessionService.upsert_session(
            session_id="s-owned", title="T", messages=EMPTY_TREE, owner_user_id="keep-me"
        )
        # dry-run：統計 1 筆、不落盤
        null_count, updated = await mod.backfill("user-x", apply=False)
        assert (null_count, updated) == (1, 0)
        s = await SessionService.get_session_by_id("s-null")
        assert s is not None and s.get("owner_user_id") is None
        # apply：填 NULL、絕不覆蓋既有
        null_count, updated = await mod.backfill("user-x", apply=True)
        assert (null_count, updated) == (1, 1)
        s_null = await SessionService.get_session_by_id("s-null")
        s_owned = await SessionService.get_session_by_id("s-owned")
        assert s_null is not None and s_null["owner_user_id"] == "user-x"
        assert s_owned is not None and s_owned["owner_user_id"] == "keep-me"
        # 冪等：再跑無事可做
        null_count, updated = await mod.backfill("user-y", apply=True)
        assert (null_count, updated) == (0, 0)
