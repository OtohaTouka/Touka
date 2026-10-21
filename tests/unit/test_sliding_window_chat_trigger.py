"""Unit tests for 聊天側滑動視窗觸發（ADR-0092 Phase D / 禁令 28 第二觸發點 / 禁令 57）。

驗證 `_maybe_trim_after_chat`：僅家對話觸發 check_and_trim、子對話/缺 session 跳過。
（無痕由 `_pump_stream_to_buffer` 的 `not incognito` 閘門擋下，不在此函式內。）

剪裁身分：一律用家對話所屬的靈魂（session 的 home_for_agent，和設家的規則同一個判定），
不用 UI 請求者（人類）；解析不到靈魂就不剪、記 warning，絕不退回人類身分。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.models.identity import AgentIdentity
from src.models.sliding_window import SlidingWindowConfig
from src.services import dispatcher as dispatcher_mod
from src.services import sliding_window_service as sliding
from src.services.dispatcher import _maybe_trim_after_chat
from src.services.resource_manager import resource_manager
from src.utils.async_db import init_db_async, session_service

_SOUL = "Hanatou"
_SS = "src.services.dispatcher.session_service"
_SW = "src.services.dispatcher.sliding_window_service"


def _passport(agent_id: str, *, soul: bool = True, normalized: str | None = None) -> dict:
    return {"agent_id": normalized or agent_id, "is_toukasoul": soul, "role": "Owner"}


@pytest.fixture
def souls(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
    """agent_service.identify_agent 的替身：只有登記在這裡的 ID 是靈魂。"""
    registry: dict[str, dict] = {_SOUL: _passport(_SOUL)}
    monkeypatch.setattr(dispatcher_mod.agent_service, "identify_agent", registry.get)
    return registry


async def test_home_session_trims_as_its_soul(souls: dict[str, dict]) -> None:
    with patch(_SS) as mock_ss, patch(_SW) as mock_sw:
        mock_ss.get_session_by_id = AsyncMock(
            return_value={"is_home": True, "home_for_agent": _SOUL, "owner_user_id": "human-1"}
        )
        mock_sw.check_and_trim = AsyncMock(return_value=True)
        await _maybe_trim_after_chat("sess-1")
    mock_sw.check_and_trim.assert_awaited_once()
    identity, session_id = mock_sw.check_and_trim.await_args.args
    assert session_id == "sess-1"
    assert isinstance(identity, AgentIdentity)
    assert identity.agent_id == _SOUL and not identity.is_human


@pytest.mark.parametrize(
    ("session", "registry", "reason"),
    [
        ({"is_home": False, "home_for_agent": None}, {}, None),  # 子對話：照舊不剪、不警告
        (None, {}, None),  # 找不到 session
        ({"is_home": True, "home_for_agent": None}, {}, "no soul"),  # 家對話卻沒有歸屬
        ({"is_home": True, "home_for_agent": "gpt-4o"}, {"gpt-4o": _passport(
            "gpt-4o", soul=False)}, "no soul"),  # 歸屬不是靈魂
        ({"is_home": True, "home_for_agent": "ToukaSoulHanatou"}, {"ToukaSoulHanatou": _passport(
            "ToukaSoulHanatou", normalized=_SOUL)}, "no soul"),  # 舊前綴：和設家規則一樣不收
        ({"is_home": True, "home_for_agent": "Ghost"}, {}, "no soul"),  # 註冊表查不到
    ],
)
async def test_no_trim_without_a_resolvable_soul(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    session: dict | None,
    registry: dict,
    reason: str | None,
) -> None:
    monkeypatch.setattr(dispatcher_mod.agent_service, "identify_agent", registry.get)
    with patch(_SS) as mock_ss, patch(_SW) as mock_sw, caplog.at_level("WARNING"):
        mock_ss.get_session_by_id = AsyncMock(return_value=session)
        mock_sw.check_and_trim = AsyncMock(return_value=True)
        await _maybe_trim_after_chat("sess-1")
    mock_sw.check_and_trim.assert_not_awaited()
    warned = [r for r in caplog.records if r.name == dispatcher_mod.__name__]
    assert bool(warned) is (reason is not None)


# ── 端到端（沙盒）：聊天側剪裁的舊對話與同步都落在靈魂名下 ─────────────────────────


@pytest.fixture
async def sessions_db(tmp_path: Path) -> Any:
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "sessions.db"):
        await init_db_async()
        yield


def _line(rounds: int) -> dict[str, Any]:
    messages: dict[str, dict[str, Any]] = {}
    parent = None
    for i in range(rounds * 2):
        node_id = f"n{i}"
        messages[node_id] = {
            "id": node_id, "role": "user" if i % 2 == 0 else "assistant",
            "content": f"line-{i}", "parentId": parent, "childrenIds": [],
            "timestamp": 1_760_486_400_000 + i * 60_000,
        }
        if parent:
            messages[parent]["childrenIds"].append(node_id)
        parent = node_id
    return {"messages": messages, "currentId": parent}


@pytest.fixture
async def home_trim(sessions_db: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """一個靈魂的家對話（人類擁有、4 輪、要剪）與聊天側剪裁所需的設定。"""
    soul_id = f"T4Soul{uuid.uuid4().hex[:6]}"
    human = AgentIdentity(agent_id=f"t4-human-{uuid.uuid4().hex[:6]}", is_human=True)
    soul = AgentIdentity.for_role(agent_id=soul_id, role="Owner", is_human=False)
    monkeypatch.setattr(
        dispatcher_mod.agent_service, "identify_agent", {soul_id: _passport(soul_id)}.get
    )
    sid = f"t4-home-{uuid.uuid4().hex[:6]}"
    assert await session_service.upsert_session(
        sid, "home", _line(4), identity=soul, owner_user_id=human.agent_id
    )
    assert await session_service.set_home(sid, soul, True)

    service = sliding.SlidingWindowService()
    monkeypatch.setattr(
        service, "load_config", lambda: SlidingWindowConfig(enabled=True, summary_budget_pct=5)
    )
    monkeypatch.setattr(service, "_current_total_tokens", lambda *_a: 100)
    monkeypatch.setattr(sliding, "get_context_window", lambda _m: 100)
    summarize = AsyncMock(return_value="summary")
    monkeypatch.setattr(sliding, "summarize_dialogue", summarize)
    monkeypatch.setattr(dispatcher_mod, "sliding_window_service", service)
    return {"sid": sid, "soul": soul, "human": human, "summarize": summarize}


async def test_chat_trim_archive_goes_to_the_soul(
    home_trim: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """記錄歸檔呼叫（不真的寫）：歸檔與摘要記帳都在靈魂名下，人類的 legacy 目錄不出現。"""
    soul, human = home_trim["soul"], home_trim["human"]
    manager = MagicMock()
    archive = manager.get_legacy_sync_service.return_value.archive_payload
    archive.return_value = 4
    monkeypatch.setattr(sliding, "get_manager", lambda: manager)

    await _maybe_trim_after_chat(home_trim["sid"])

    identity, payload, _label = archive.call_args.args
    assert identity.agent_id == soul.agent_id and not identity.is_human
    assert payload[0]["agent_id"] == soul.agent_id
    assert home_trim["summarize"].await_args.args[0].agent_id == soul.agent_id  # 摘要記帳歸靈魂
    assert not Path(resource_manager.get_vault_path("legacy", human)).exists()
    trimmed = await session_service.get_session_by_id(home_trim["sid"])
    assert "n0" not in trimmed["messages"]["messages"]


async def test_chat_trim_archive_and_manual_sync_on_the_real_resource_manager(
    home_trim: dict[str, Any],
) -> None:
    """不換假檔案層：剪裁 → 歸檔進 legacy/<靈魂>/ 的舊對話庫 → 手動同步使用者放的檔 → 重跑。"""
    soul, human = home_trim["soul"], home_trim["human"]
    await _maybe_trim_after_chat(home_trim["sid"])

    trimmed = await session_service.get_session_by_id(home_trim["sid"])
    assert "n0" not in trimmed["messages"]["messages"]
    assert trimmed["context_summary"] == "summary"
    soul_dir = Path(resource_manager.get_vault_path("legacy", soul))
    assert not list(soul_dir.glob("*.json"))  # 系統沒有在 legacy/ 寫任何檔
    sync = sliding.get_manager().get_legacy_sync_service()
    store = sync.get_store(soul)
    assert store.get_message("n0") is not None and store.get_message("n3") is not None

    # 使用者自己放進來的舊匯出檔（沙盒）：同步成功、不搬檔；重跑跳過、不多一份
    owner_file = soul_dir / "owner-export.json"
    owner_file.write_text(json.dumps({"id": "old", "title": "old", "messages": [
        {"id": "o1", "role": "user", "content": "from an old export"}]}), encoding="utf-8")
    first = sync.sync_all_json_to_db(soul)
    second = sync.sync_all_json_to_db(soul)
    assert (first["synced"], first["errors"]) == (1, 0)
    assert (second["synced"], second["skipped"], second["errors"]) == (0, 1, 0)
    assert second["total_in_db"] == first["total_in_db"]
    assert owner_file.exists() and not (soul_dir / "backup").exists()
    assert not Path(resource_manager.get_vault_path("legacy", human)).exists()
