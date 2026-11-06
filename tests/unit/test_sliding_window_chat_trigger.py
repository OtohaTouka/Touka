"""Unit tests for 聊天側滑動視窗觸發（ADR-0092 Phase D / 禁令 28 第二觸發點 / 禁令 57）。

驗證 `_maybe_trim_after_chat`：僅家對話觸發 check_and_trim、子對話/缺 session 跳過。
（無痕由 `_pump_stream_to_buffer` 的 `not incognito` 閘門擋下，不在此函式內。）

剪裁身分：一律用家對話所屬的靈魂（session 的 home_for_agent，和設家的規則同一個判定），
不用 UI 請求者（人類）；解析不到靈魂就不剪、記 warning，絕不退回人類身分。
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.models.identity import AgentIdentity
from src.models.sliding_window import SlidingWindowConfig
from src.routers import ui_sessions
from src.services import chat_trim_wait, destructive_ops
from src.services import dispatcher as dispatcher_mod
from src.services import sliding_window_service as sliding
from src.services.chat_trim_wait import closing_saves
from src.services.dispatcher import _maybe_trim_after_chat
from src.services.resource_manager import resource_manager
from src.services.stream_buffer_service import stream_buffer_service
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
    human = AgentIdentity.for_role(  # 擁有這個靈魂，畫面的保存才進得去
        f"t4-human-{uuid.uuid4().hex[:6]}", "normal", is_human=True, owned_agents=[soul_id]
    )
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


# ── 先等畫面的收尾保存再剪（ADR-0302）──────────────────────────────────────────────


async def _ui_save(home_trim: dict[str, Any], **messages_changes: Any) -> Any:
    """畫面整棵存一次（走真的保存端點，存好時會叫醒等著的剪裁）。"""
    sid, human = home_trim["sid"], home_trim["human"]
    snapshot = await session_service.read_persistence_snapshot(sid, human)
    tree = copy.deepcopy(snapshot.session["messages"])
    tree["messages"].update(messages_changes.get("nodes", {}))
    tree["currentId"] = messages_changes.get("current", tree["currentId"])
    model = ui_sessions.SessionModel(
        id=sid, title="home", messages=tree, updatedAt=2000, revision=snapshot.revision
    )
    return await ui_sessions.save_session(sid, model, human)


def _rich_reply() -> dict[str, Any]:
    return {"n7": {"id": "n7", "role": "assistant", "content": "line-7", "parentId": "n6",
                   "childrenIds": [], "timestamp": 1_760_486_820_000, "steps": [{"tool": "x"}]}}


async def _waiting_turn(home_trim: dict[str, Any]) -> asyncio.Task[None]:
    sid = home_trim["sid"]
    revision = (await session_service.read_persistence_snapshot(sid, home_trim["human"])).revision
    waiter = closing_saves.register(sid, revision, "n7", home_trim["human"])
    task = asyncio.create_task(_maybe_trim_after_chat(sid, waiter))
    await asyncio.sleep(0.05)
    return task


async def test_chat_trim_waits_for_the_closing_save_and_keeps_its_rich_reply(
    home_trim: dict[str, Any],
) -> None:
    task = await _waiting_turn(home_trim)
    assert not task.done() and home_trim["summarize"].await_count == 0

    assert (await _ui_save(home_trim, nodes=_rich_reply())).persistence.status == "saved"
    await asyncio.wait_for(task, 5)

    trimmed = await session_service.get_session_by_id(home_trim["sid"])
    assert "n0" not in trimmed["messages"]["messages"]
    assert trimmed["messages"]["messages"]["n7"]["steps"] == [{"tool": "x"}]
    home_trim["summarize"].assert_awaited_once()


async def test_chat_trim_trims_the_text_version_when_the_closing_save_never_comes(
    home_trim: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_trim_wait, "CLOSING_SAVE_WAIT_SECONDS", 0.05)
    await asyncio.wait_for(await _waiting_turn(home_trim), 5)
    trimmed = await session_service.get_session_by_id(home_trim["sid"])
    assert "n0" not in trimmed["messages"]["messages"]


async def test_a_closing_save_before_the_wait_starts_is_not_missed(
    home_trim: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """畫面在剪裁開始等之前就存好了：不等到逾時，直接往下剪。"""
    monkeypatch.setattr(chat_trim_wait, "CLOSING_SAVE_WAIT_SECONDS", 60)
    sid = home_trim["sid"]
    revision = (await session_service.read_persistence_snapshot(sid, home_trim["human"])).revision
    assert (await _ui_save(home_trim, nodes=_rich_reply())).persistence.status == "saved"
    waiter = closing_saves.register(sid, revision, "n7", home_trim["human"])
    await asyncio.wait_for(_maybe_trim_after_chat(sid, waiter), 5)
    assert "n0" not in (await session_service.get_session_by_id(sid))["messages"]["messages"]


async def test_a_newer_turn_replaces_the_waiting_trim(home_trim: dict[str, Any]) -> None:
    sid = home_trim["sid"]
    task = await _waiting_turn(home_trim)
    newer = closing_saves.register(sid, "newer", "n9", home_trim["human"])
    await asyncio.wait_for(task, 1)
    assert "n0" in (await session_service.get_session_by_id(sid))["messages"]["messages"]
    home_trim["summarize"].assert_not_awaited()
    closing_saves.saved(sid)
    assert await closing_saves.wait(newer)


async def test_a_replaced_trim_does_not_wake_the_newer_one(home_trim: dict[str, Any]) -> None:
    """舊的那輪發現存檔已經變了：只放掉自己，不能替新的一輪宣告「畫面存好了」。"""
    sid = home_trim["sid"]
    older = closing_saves.register(sid, "stale", "n7", home_trim["human"])
    newer = closing_saves.register(sid, "newer", "n9", home_trim["human"])
    await asyncio.wait_for(_maybe_trim_after_chat(sid, older), 5)
    assert not newer.saved.is_set()
    closing_saves.saved(sid)
    assert await closing_saves.wait(newer)


@pytest.mark.parametrize(
    "lookup",
    [AsyncMock(return_value={"is_home": False}), AsyncMock(side_effect=RuntimeError("db down"))],
    ids=["not_home", "read_fails"],
)
async def test_the_waiter_is_cleared_when_there_is_nothing_to_trim(lookup: AsyncMock) -> None:
    """不是家或讀檔出錯：等待者也要清掉，下一輪登記時不會以為還有人在等。"""
    sid = f"sess-{uuid.uuid4().hex[:6]}"
    human = AgentIdentity.for_role("h", "normal", is_human=True)
    waiter = closing_saves.register(sid, "r1", "n7", human)
    with patch(_SS) as mock_ss, patch(_SW) as mock_sw:
        mock_ss.get_session_by_id = lookup
        mock_sw.check_and_trim = AsyncMock(return_value=True)
        with contextlib.suppress(RuntimeError):
            await _maybe_trim_after_chat(sid, waiter)
    newer = closing_saves.register(sid, "r2", "n9", human)
    assert not waiter.replaced
    mock_sw.check_and_trim.assert_not_awaited()
    closing_saves.saved(sid)
    assert await closing_saves.wait(newer)


@pytest.mark.parametrize("busy", ["heartbeat", "next_message", "pending_request"])
async def test_chat_trim_gives_up_when_the_session_moved_on(
    home_trim: dict[str, Any], busy: str
) -> None:
    sid, soul, human = home_trim["sid"], home_trim["soul"], home_trim["human"]
    task = await _waiting_turn(home_trim)
    with contextlib.ExitStack() as stack:
        if busy == "heartbeat":  # 心跳結束時自己會剪
            destructive_ops.start_heartbeat(soul, sid)
            stack.callback(destructive_ops.end_heartbeat, soul, sid)
        if busy == "pending_request":  # 下一則已經送到、還沒開始串流
            stack.enter_context(stream_buffer_service.pending_request(sid, human))
        nodes = _rich_reply()
        current = "n7"
        if busy == "next_message":  # 您已經存下一則 user
            nodes["n7"]["childrenIds"] = ["n8"]
            nodes["n8"] = {"id": "n8", "role": "user", "content": "next", "parentId": "n7",
                           "childrenIds": [], "timestamp": 1_760_486_880_000}
            current = "n8"
        saved = await _ui_save(home_trim, nodes=nodes, current=current)
        assert saved.persistence.status == "saved"
        await asyncio.wait_for(task, 5)
    assert "n0" in (await session_service.get_session_by_id(sid))["messages"]["messages"]
    home_trim["summarize"].assert_not_awaited()


@pytest.mark.parametrize("stage", ["summary", "commit"])
async def test_chat_trim_checks_again_before_the_summary_and_the_commit(
    home_trim: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """讀完存檔之後下一則才送到：寫摘要前、存回前各再查一次，省下白花的模型呼叫。"""
    sid, human = home_trim["sid"], home_trim["human"]
    manager = MagicMock()
    archive = manager.get_legacy_sync_service.return_value.archive_payload
    archive.return_value = 4
    monkeypatch.setattr(sliding, "get_manager", lambda: manager)
    pending = contextlib.ExitStack()

    def arrive(result: Any) -> Any:
        pending.enter_context(stream_buffer_service.pending_request(sid, human))
        return result

    if stage == "summary":
        archive.side_effect = lambda *_a: arrive(4)
    else:
        home_trim["summarize"].side_effect = lambda *_a: arrive("summary")
    task = await _waiting_turn(home_trim)
    with pending:
        assert (await _ui_save(home_trim, nodes=_rich_reply())).persistence.status == "saved"
        await asyncio.wait_for(task, 5)
    assert "n0" in (await session_service.get_session_by_id(sid))["messages"]["messages"]
    assert home_trim["summarize"].await_count == (0 if stage == "summary" else 1)


async def test_trims_that_lose_twice_do_not_duplicate_the_archive(
    home_trim: dict[str, Any],
) -> None:
    """ADR-0295 曾寫「舊對話庫每次多一份」：剪裁輸掉兩次，舊對話庫裡的訊息數不變。"""
    soul = home_trim["soul"]
    store = sliding.get_manager().get_legacy_sync_service().get_store(soul)
    stats = []

    async def lose(*_args: Any) -> str:  # 寫摘要的時候畫面先存了，存回時版本對不上
        await _ui_save(home_trim, nodes=_rich_reply())
        return "summary"

    home_trim["summarize"].side_effect = lose
    for _ in range(2):
        await _maybe_trim_after_chat(home_trim["sid"])
        stats.append(store.get_stats())
    untrimmed = await session_service.get_session_by_id(home_trim["sid"])
    assert "n0" in untrimmed["messages"]["messages"]
    assert stats[0] == stats[1] and stats[0]["message_count"] > 0
