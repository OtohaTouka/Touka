"""家標記不算進存檔版本（ADR-0301）：設家、換家、取消家不讓別處手上的版本失效。

存檔一律在交易裡重讀最新的那一列、只改要改的欄位，舊版本存進來也蓋不掉新家；刪除與換綁
看的也是最新的那一列。剪裁只剪家對話，讀檔時與存回時各確認一次。

全部離線：SQLite 只在測試沙盒；模型串流、舊對話庫、摘要與 hook 都是假的。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from src.models.context import PipelineContext
from src.models.session_persistence import SessionChanges
from src.models.sliding_window import SlidingWindowConfig
from src.models.souls import SoulConfig
from src.routers import heartbeat as hb
from src.services import sliding_window_service as sliding
from src.services.soul_service import SoulService
from src.utils import async_db

from tests.support.session_scope import HUMAN, A, B, raw_row, seed
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]

service = async_db.session_service


async def snap(sid="chat"):
    return await service.read_persistence_snapshot(sid, HUMAN)


async def home_of(sid="chat"):
    row = await raw_row(sid)
    return row["is_home"], row["home_for_agent"]


@pytest.mark.parametrize(
    ("home_before", "set_home", "expected"),
    [
        (None, ("chat", True), (1, A.agent_id)),
        ("chat", ("other", True), (0, None)),
        ("chat", ("chat", False), (0, None)),
    ],
    ids=["set", "move", "clear"],
)
async def test_home_change_keeps_old_revision_and_the_new_home(home_before, set_home, expected):
    await seed("chat")
    await seed("other")
    if home_before:
        assert await service.set_home(home_before, A, True)
    stale = await snap()
    assert await service.set_home(set_home[0], A, set_home[1])
    assert (await snap()).revision == stale.revision
    result = await service.conditional_save_session(
        "chat", stale.revision, SessionChanges(title="renamed"), HUMAN
    )
    assert result.status == "saved"
    assert (await raw_row())["title"] == "renamed"
    assert await home_of() == expected


@pytest.mark.parametrize(
    ("home", "changes"),
    [
        ("chat", {"is_home": False, "home_for_agent": None}),
        ("other", {"is_home": True, "home_for_agent": A}),
    ],
    ids=["clear", "second-home"],
)
async def test_a_versioned_save_cannot_change_the_home_fields(home, changes):
    """家標記只由設家改（它會先清掉同一個靈魂的舊家）；帶版本的存檔改它一律擋下。"""
    await seed("chat")
    await seed("other")
    assert await service.set_home(home, A, True)
    before = {sid: await raw_row(sid) for sid in ("chat", "other")}
    current = await snap()
    result = await service.conditional_save_session(
        "chat", current.revision, SessionChanges(title="renamed", **changes), HUMAN
    )
    assert result.status == "failed"
    assert {sid: await raw_row(sid) for sid in ("chat", "other")} == before


async def test_old_revision_still_cannot_delete_the_new_home():
    await seed("chat")
    stale = await snap()
    assert await service.set_home("chat", A, True)
    result = await service.conditional_delete_session("chat", stale.revision, HUMAN)
    assert result.status == "failed"
    assert await home_of() == (1, A.agent_id)


async def test_old_revision_still_cannot_rebind_the_new_home():
    await seed("chat")
    stale = await snap()
    assert await service.set_home("chat", A, True)
    result = await service.save_ui_session("chat", stale.revision, SessionChanges(), HUMAN, B)
    assert result.status == "failed"
    assert (await raw_row())["agent_id"] == A.agent_id
    assert await home_of() == (1, A.agent_id)


@pytest.fixture
def trim(monkeypatch):
    """心跳結尾用的同一個剪裁服務，設定成一定會剪；舊對話庫、摘要與 hook 換成假的。"""
    svc = sliding.sliding_window_service
    monkeypatch.setattr(
        svc, "load_config", lambda: SlidingWindowConfig(enabled=True, summary_budget_pct=5)
    )
    monkeypatch.setattr(svc, "_resolve_model", lambda _linear: "synthetic-model")
    monkeypatch.setattr(svc, "_check_model_cooldown", lambda *_a: False)
    monkeypatch.setattr(svc, "_current_total_tokens", lambda *_a: 100)
    monkeypatch.setattr(sliding, "get_context_window", lambda _model: 100)
    monkeypatch.setattr(
        svc, "compute_split_by_tokens", lambda linear, *_a: (linear[:1], linear[1:])
    )
    archive = Mock(return_value=1)
    manager = SimpleNamespace(
        get_legacy_sync_service=lambda: SimpleNamespace(archive_payload=archive)
    )
    monkeypatch.setattr(sliding, "get_manager", lambda: manager)
    doubles = SimpleNamespace(
        service=svc,
        archive=archive,
        summarize=AsyncMock(return_value="new summary"),
        pre_compact=Mock(),
    )
    monkeypatch.setattr(sliding, "summarize_dialogue", doubles.summarize)
    monkeypatch.setattr(sliding, "notify_pre_compact", doubles.pre_compact)
    monkeypatch.setattr(sliding, "notify_post_compact", Mock())
    return doubles


def assert_not_trimmed(trim):
    trim.archive.assert_not_called()
    trim.summarize.assert_not_awaited()
    trim.pre_compact.assert_not_called()


async def test_trim_still_trims_the_home_session(trim):
    await seed("chat")
    assert await service.set_home("chat", A, True)
    assert await trim.service.check_and_trim(A, "chat") is True
    trim.archive.assert_called_once()
    assert list(json.loads((await raw_row())["messages"])["messages"]) == ["a"]


async def test_trim_skips_a_session_that_is_not_home_when_read(trim):
    await seed("chat")
    before = await raw_row()
    assert await trim.service.check_and_trim(A, "chat") is False
    assert_not_trimmed(trim)
    assert await raw_row() == before


@pytest.mark.parametrize("set_home", [("chat", False), ("other", True)], ids=["clear", "move"])
async def test_trim_write_is_refused_when_home_changes_after_the_read(trim, set_home):
    await seed("chat")
    await seed("other")
    assert await service.set_home("chat", A, True)
    before = await raw_row()

    async def change_home(*_args, **_kwargs):
        assert await service.set_home(set_home[0], A, set_home[1])
        return "new summary"

    trim.summarize.side_effect = change_home
    assert await trim.service.check_and_trim(A, "chat") is False
    trim.archive.assert_called_once()
    row = await raw_row()
    assert row["messages"] == before["messages"]
    assert row["context_summary"] == before["context_summary"]
    assert row["is_home"] == 0


async def test_heartbeat_keeps_writing_after_home_moves_and_skips_the_end_trim(monkeypatch, trim):
    await seed("chat")
    await seed("other")
    assert await service.set_home("chat", A, True)
    await service.check_old_home("chat", mark_started=True, requester=None)  # /awaken 記的

    async def body():
        assert await service.set_home("other", A, True)  # 心跳跑到一半換家
        yield 'data: {"choices":[{"delta":{"content":"pulse reply"}}]}\n\n'

    async def dispatch(**_kwargs):
        return MagicMock(body_iterator=body())

    soul = object.__new__(SoulService)  # 不污染 singleton
    soul.get_soul_data = lambda _identity: ("靈魂", SoulConfig().model_dump())
    autonomy = MagicMock()
    autonomy.check_quota.return_value = True
    autonomy.is_over_budget.return_value = False
    monkeypatch.setattr(hb, "dispatch_request", dispatch)
    monkeypatch.setattr(hb, "soul_service", soul)
    monkeypatch.setattr(hb, "autonomy_service", autonomy)
    monkeypatch.setattr(hb, "rhythm_service", MagicMock())
    monkeypatch.setattr(hb, "_pulse_model_name", lambda *_a: "synthetic-model")
    monkeypatch.setattr(hb.model_manager, "check_models_health", AsyncMock())
    await hb._process_heartbeat_loop(
        final_session_id="chat",
        identity=A,
        model_override=None,
        ctx=PipelineContext(),
        pulse_count=1,
        pulse_slots=1,
        t0=0.0,
        init_prompt="",
        is_home_mode=True,
    )
    nodes = json.loads((await raw_row())["messages"])["messages"].values()
    assert "pulse reply" in [node["content"] for node in nodes]
    assert_not_trimmed(trim)
    assert await home_of() == (0, None)
    # 已經在跑的這一輪寫得完；換家之後原段是舊家，新的一輪在入口就被擋（ADR-0306）
    assert await service.check_old_home("chat", mark_started=False, requester=A)
