"""ADR-0113 — Per-Agent 資料重置（TDD，安全優先）。

涵蓋：
  - assert_agent_resettable 守衛：接受真實被擁有 agent、拒絕 DebugAgent/未知/未擁有、正規化、
    fail-closed（列舉失敗→拒絕）。
  - reset_agent_data 協調器：經守衛驗證、驅動 stores、備份優先、回報。
  - 端點安全閘：admin/CSRF/密碼重輸/雙重確認。
  - 🛡️ 關鍵安全回歸：custom agent 的 config.json／system_prompt.md（身分）於重置後**存活**，
    僅 drive/outputs（執行期）被清。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.routers import admin as A
from src.services.agent_reset import guard as G
from src.services.agent_reset import orchestrator as O
from src.services.agent_reset import primitives as P
import src.services.resource_manager as RM
from src.services.agent_service import agent_service
from src.services.resource_manager import resource_manager
from src.utils import soul_io
from src.utils.async_db import get_system_setting, init_db_async
from src.utils.continuity_events_db import (
    EventRow,
    agent_epoch_key,
    count_agent_events,
    insert_event,
)


def _admin() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="admin1", role="admin")


# ── 守衛 assert_agent_resettable ──────────────────────────────────────────────

def test_guard_accepts_real_owned_agent(monkeypatch):
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: ["Jin"])
    monkeypatch.setattr(resource_manager, "is_owner", lambda t, a: True)
    assert G.assert_agent_resettable("Jin", _admin()) == "Jin"


def test_guard_normalizes_toukasoul_prefix(monkeypatch):
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: ["Jin"])
    monkeypatch.setattr(resource_manager, "is_owner", lambda t, a: True)
    assert G.assert_agent_resettable("ToukaSoulJin", _admin()) == "Jin"


def test_guard_rejects_debugagent():
    """DebugAgent 走自己的 ADR-0104 路徑；通用路徑硬性排除（系統保護）。"""
    with pytest.raises(ToukaException):
        G.assert_agent_resettable("DebugAgent", _admin())


def test_guard_rejects_empty():
    with pytest.raises(ToukaException):
        G.assert_agent_resettable("   ", _admin())


def test_guard_rejects_unknown_agent(monkeypatch):
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: [])
    monkeypatch.setattr(agent_service, "identify_agent", lambda c: None)
    with pytest.raises(ToukaException):
        G.assert_agent_resettable("Ghost", _admin())


def test_guard_rejects_not_owned(monkeypatch):
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: ["Jin"])
    monkeypatch.setattr(resource_manager, "is_owner", lambda t, a: False)
    actor = AgentIdentity.for_role(agent_id="user2", role="Base")
    with pytest.raises(ToukaException):
        G.assert_agent_resettable("Jin", actor)


def test_guard_fail_closed_on_enumeration_error(monkeypatch):
    """列舉靈魂失敗 → 無法確認是真實 agent → fail-closed 拒絕（不對不確定目標動手）。"""
    def boom():
        raise RuntimeError
    monkeypatch.setattr(soul_io, "list_soul_ids", boom)
    monkeypatch.setattr(agent_service, "identify_agent", lambda c: None)
    with pytest.raises(ToukaException):
        G.assert_agent_resettable("Jin", _admin())


# ── 協調器 reset_agent_data ───────────────────────────────────────────────────

async def test_reset_agent_data_drives_stores_with_guard(monkeypatch):
    monkeypatch.setattr(O, "assert_agent_resettable", lambda c, a: c)  # pass-through
    calls: list[str] = []

    async def fake_purge(candidate, guard):
        calls.append(candidate)
        return 3

    async def fake_remaining(candidate, guard):
        return 0

    fake = P.StorePurger("fake", fake_purge, fake_remaining)
    monkeypatch.setattr(O, "ALL_STORES", (fake,))
    monkeypatch.setattr(O.system_backup, "create_backup", lambda: {"status": "ok"})

    report = await O.reset_agent_data("Jin", _admin(), do_backup=True)
    assert report.agent == "Jin"
    assert report.clean is True
    assert calls == ["Jin"]
    assert report.backup == {"status": "ok"}


async def test_reset_agent_data_false_clean_on_residue(monkeypatch):
    monkeypatch.setattr(O, "assert_agent_resettable", lambda c, a: c)

    async def fake_purge(candidate, guard):
        return 1

    async def fake_remaining(candidate, guard):
        return 2  # 殘留 → false-clean

    fake = P.StorePurger("fake", fake_purge, fake_remaining)
    monkeypatch.setattr(O, "ALL_STORES", (fake,))
    report = await O.reset_agent_data("Jin", _admin(), do_backup=False)
    assert report.clean is False


# ── 端點安全閘 ────────────────────────────────────────────────────────────────

async def test_endpoint_rejects_without_confirm(monkeypatch):
    monkeypatch.setattr(A, "verify_origin", lambda r: None)
    req = A.AgentResetRequest(confirm=False, confirm_agent_id="Jin", current_password="x")
    user = SimpleNamespace(role="admin", hashed_password="h", id="admin1")
    with pytest.raises(ToukaException):
        await A.reset_agent("Jin", req, None, _admin(), user, Mock())


async def test_endpoint_rejects_mismatched_confirm_id(monkeypatch):
    monkeypatch.setattr(A, "verify_origin", lambda r: None)
    req = A.AgentResetRequest(confirm=True, confirm_agent_id="WrongName", current_password="x")
    user = SimpleNamespace(role="admin", hashed_password="h", id="admin1")
    with pytest.raises(ToukaException):
        await A.reset_agent("Jin", req, None, _admin(), user, Mock())


async def test_endpoint_rejects_wrong_password(monkeypatch):
    monkeypatch.setattr(A, "verify_origin", lambda r: None)
    auth = Mock()
    auth.verify_password.return_value = False
    req = A.AgentResetRequest(confirm=True, confirm_agent_id="Jin", current_password="wrong")
    user = SimpleNamespace(role="admin", hashed_password="h", id="admin1")
    with pytest.raises(ToukaException):
        await A.reset_agent("Jin", req, None, _admin(), user, auth)


async def test_endpoint_happy_path(monkeypatch):
    monkeypatch.setattr(A, "verify_origin", lambda r: None)
    auth = Mock()
    auth.verify_password.return_value = True

    async def fake_reset(target, actor, **kw):
        return O.ResetReport(agent=target, clean=True)

    monkeypatch.setattr(A, "reset_agent_data", fake_reset)
    req = A.AgentResetRequest(confirm=True, confirm_agent_id="Jin", current_password="good")
    user = SimpleNamespace(role="admin", hashed_password="h", id="admin1")
    out = await A.reset_agent("Jin", req, None, _admin(), user, auth)
    assert out["agent_id"] == "Jin"
    assert out["clean"] is True


async def test_timeline_store_is_scoped_to_the_real_agent(monkeypatch, tmp_path):
    """per-agent 重置的 timeline 列：只清目標靈魂的活動事件、其他靈魂不動，並寫它的起點。"""
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: ["Jin", "Rei"])
    monkeypatch.setattr(resource_manager, "is_owner", lambda t, a: True)
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "sessions.db"):
        await init_db_async()
        for agent in ("Jin", "Jin", "Rei"):
            await insert_event(
                EventRow(
                    event_id=f"user_turn:{agent}:{uuid.uuid4().hex}",
                    agent=agent,
                    kind="user_turn",
                    occurred_at=1,
                    subject="m",
                )
            )

        def guard(c: str) -> str:
            return G.assert_agent_resettable(c, _admin())

        assert await P.purge_timeline("Jin", guard) == 2
        assert await P.remaining_timeline("Jin", guard) == 0
        assert await count_agent_events(AgentIdentity(agent_id="Rei")) == 1
        assert await get_system_setting(agent_epoch_key(AgentIdentity(agent_id="Jin")))
        with pytest.raises(ToukaException):
            await P.purge_timeline("DebugAgent", guard)


# ── 🛡️ 關鍵安全回歸：custom agent 身分（config.json/system_prompt.md）存活 ──────

async def test_custom_agent_identity_survives_reset(monkeypatch, tmp_path):
    """重置只清 drive/outputs（執行期）；custom config.json／system_prompt.md（身分）永不被觸及。"""
    monkeypatch.setattr(RM, "get_app_data_dir", lambda: str(tmp_path))

    agent = "TestCustomAgent"
    custom = tmp_path / "custom" / agent
    (custom / "drive" / "outputs").mkdir(parents=True)
    (custom / "config.json").write_text('{"base_model":"x"}', encoding="utf-8")
    (custom / "system_prompt.md").write_text("identity-do-not-touch", encoding="utf-8")
    (custom / "drive" / "outputs" / "artifact.txt").write_text("runtime", encoding="utf-8")
    (custom / "drive" / "missions").mkdir(parents=True)
    (custom / "drive" / "missions" / "current_mission.json").write_text("{}", encoding="utf-8")

    def guard(c: str) -> str:  # pass-through（本測試聚焦原語的路徑範圍，非守衛）
        return c

    purged_outputs = await P.purge_outputs(agent, guard)
    purged_mission = await P.purge_mission(agent, guard)

    assert purged_outputs >= 1
    assert purged_mission >= 1
    # 執行期已清
    assert not (custom / "drive" / "outputs").exists()
    assert not (custom / "drive" / "missions").exists()
    # 身分永存（最關鍵的安全不變式）
    assert (custom / "config.json").exists()
    assert (custom / "config.json").read_text(encoding="utf-8") == '{"base_model":"x"}'
    assert (custom / "system_prompt.md").exists()
    assert (custom / "system_prompt.md").read_text(encoding="utf-8") == "identity-do-not-touch"
