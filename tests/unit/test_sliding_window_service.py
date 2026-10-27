"""Unit tests for SlidingWindowService (TDD — RED before implementation).

Tests cover pure functions (count_rounds, should_trim, compute_split) and
the async check_and_trim method with full mocking for ACID safety scenarios.

All tests use DebugAgent as identity; no real session/file I/O is performed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from memory_server.services import legacy_store as legacy_store_module
from memory_server.services.legacy_store import LegacyStore
from memory_server.services.legacy_sync import LegacySyncService
from memory_server.services.manager import MemoryManager
from src.models.identity import AgentIdentity
from src.models.session_persistence import SavedResult, UnknownResult
from src.models.sliding_window import SlidingWindowConfig
from src.services import sliding_window_service as sliding
from src.services.sliding_window_service import SlidingWindowService
from src.utils.async_db import SessionSnapshot

# ── helpers ────────────────────────────────────────────────────────────────

DEBUG_IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", is_human=False)
_sqlite3 = legacy_store_module.sqlite3  # 只讀 tmp_path 的舊對話庫（同 test_legacy_archive）


def _msgs(*roles: str) -> list[dict]:
    """Build a minimal linear message list with the given roles."""
    out = []
    for i, role in enumerate(roles):
        out.append(
            {
                "id": f"m{i}",
                "role": role,
                "content": f"content_{i}",
                "parentId": f"m{i - 1}" if i > 0 else None,
            }
        )
    return out


def _make_tree(linear: list[dict]) -> dict:
    """Pack a linear list into a minimal TreeHistory dict."""
    if not linear:
        return {"messages": {}, "currentId": None}
    msgs_map = {m["id"]: m for m in linear}
    return {"messages": msgs_map, "currentId": linear[-1]["id"]}


def _make_session(linear: list[dict]) -> dict:
    """Build a minimal session dict (mirrors async_db.get_session_by_id output)."""
    return {
        "id": "test-session",
        "title": "Test",
        "messages": _make_tree(linear),
    }


@pytest.fixture(autouse=True)
def archive(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """剪下的對話直接歸檔進舊對話庫：換成假的並記錄呼叫（不寫沙盒的舊對話庫）。"""
    manager = MagicMock()
    payload = manager.get_legacy_sync_service.return_value.archive_payload
    payload.return_value = 2
    monkeypatch.setattr(sliding, "get_manager", lambda: manager)
    return payload


# ── count_rounds ───────────────────────────────────────────────────────────


def test_count_rounds_empty():
    svc = SlidingWindowService()
    assert svc.count_rounds([]) == 0


def test_count_rounds_complete_pairs():
    svc = SlidingWindowService()
    msgs = _msgs("user", "assistant", "user", "assistant")
    assert svc.count_rounds(msgs) == 2


def test_count_rounds_trailing_user_not_counted():
    """Trailing user message without a following assistant is not a complete round."""
    svc = SlidingWindowService()
    msgs = _msgs("user", "assistant", "user")  # last user has no response yet
    assert svc.count_rounds(msgs) == 1


def test_count_rounds_skips_non_conversation_roles():
    """System/nudge messages between pairs should not break round counting."""
    svc = SlidingWindowService()
    msgs = _msgs("user", "assistant", "system", "user", "assistant")
    assert svc.count_rounds(msgs) == 2


# ── should_trim ────────────────────────────────────────────────────────────


def test_should_trim_below_trigger():
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=True, trigger_pct=90, target_floor_pct=50)
    assert svc.should_trim(89.0, cfg) is False


def test_should_trim_at_trigger():
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=True, trigger_pct=90, target_floor_pct=50)
    assert svc.should_trim(90.0, cfg) is True


def test_should_trim_above_trigger():
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=True, trigger_pct=90, target_floor_pct=50)
    assert svc.should_trim(97.5, cfg) is True


def test_should_trim_disabled():
    """When enabled=False, trim is always suppressed regardless of usage."""
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=False, trigger_pct=90, target_floor_pct=50)
    assert svc.should_trim(100.0, cfg) is False


# ── compute_split ──────────────────────────────────────────────────────────


def test_compute_split_correct_partition():
    """len(legacy) + len(kept) == len(original)."""
    svc = SlidingWindowService()
    # 3 complete rounds (6 messages)
    msgs = _msgs("user", "assistant", "user", "assistant", "user", "assistant")
    legacy, kept = svc.compute_split(msgs, keep_rounds=2)
    assert len(legacy) + len(kept) == len(msgs)


def test_compute_split_keeps_requested_rounds():
    svc = SlidingWindowService()
    # 3 rounds → keep 2 → legacy has 1 round (2 msgs), kept has 2 rounds (4 msgs)
    msgs = _msgs("user", "assistant", "user", "assistant", "user", "assistant")
    legacy, kept = svc.compute_split(msgs, keep_rounds=2)
    assert svc.count_rounds(kept) == 2
    assert svc.count_rounds(legacy) == 1


def test_compute_split_no_partial_rounds():
    """Split point must be on a complete pair boundary, never mid-pair."""
    svc = SlidingWindowService()
    msgs = _msgs("user", "assistant", "user", "assistant", "user", "assistant")
    legacy, kept = svc.compute_split(msgs, keep_rounds=2)
    # First message of 'kept' must be a user message (start of a round)
    assert kept[0]["role"] == "user"


def test_compute_split_kept_messages_anchored():
    """First message of the kept segment must have parentId = None."""
    svc = SlidingWindowService()
    msgs = _msgs("user", "assistant", "user", "assistant", "user", "assistant")
    _, kept = svc.compute_split(msgs, keep_rounds=2)
    assert kept[0]["parentId"] is None


def test_compute_split_nothing_to_trim():
    """If rounds ≤ keep_rounds, legacy is empty and kept is full."""
    svc = SlidingWindowService()
    msgs = _msgs("user", "assistant", "user", "assistant")
    legacy, kept = svc.compute_split(msgs, keep_rounds=5)
    assert legacy == []
    assert len(kept) == len(msgs)


# ── check_and_trim (async, ACID safety) ────────────────────────────────────


@pytest.mark.asyncio
async def test_check_and_trim_disabled_noop():
    """When config.enabled=False, check_and_trim returns False immediately."""
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=False, keep_rounds=2, upper_limit_rounds=3)
    with patch.object(svc, "load_config", return_value=cfg):
        result = await svc.check_and_trim(DEBUG_IDENTITY, "sess-1")
    assert result is False


@pytest.mark.asyncio
async def test_check_and_trim_below_limit_noop(archive: MagicMock):
    """Session below upper_limit → returns False, no writes."""
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=True, keep_rounds=2, upper_limit_rounds=5)

    # 3 complete rounds (below upper_limit=5)
    linear = _msgs("user", "assistant", "user", "assistant", "user", "assistant")
    session = _make_session(linear)

    with (
        patch.object(svc, "load_config", return_value=cfg),
        patch("src.services.sliding_window_service.session_service") as mock_ss,
    ):
        mock_ss.read_persistence_snapshot = AsyncMock(
            return_value=SessionSnapshot(session, "before")
        )
        result = await svc.check_and_trim(DEBUG_IDENTITY, "sess-1")

    assert result is False
    archive.assert_not_called()
    mock_ss.conditional_save_session.assert_not_called()


@pytest.mark.asyncio
async def test_check_and_trim_above_limit_writes_legacy_then_updates_session(
    archive: MagicMock,
):
    """ACID Step A then B: legacy written first, session updated after."""
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=True, keep_rounds=2, upper_limit_rounds=3)

    # 4 complete rounds (above upper_limit=3)
    linear = _msgs(
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    )
    session = _make_session(linear)

    call_order: list[str] = []

    with (
        patch.object(svc, "load_config", return_value=cfg),
        patch("src.services.sliding_window_service.session_service") as mock_ss,
        patch("src.services.sliding_window_service.get_context_window", return_value=10),
    ):
        mock_ss.read_persistence_snapshot = AsyncMock(
            return_value=SessionSnapshot(session, "before")
        )
        mock_ss.conditional_save_session = AsyncMock(
            return_value=SavedResult(status="saved", representation="full", revision="after")
        )
        archive.side_effect = lambda *a, **k: call_order.append("legacy") or 2
        mock_ss.conditional_save_session.side_effect = lambda *a, **k: (
            call_order.append("session")
            or SavedResult(status="saved", representation="full", revision="after")
        )

        result = await svc.check_and_trim(DEBUG_IDENTITY, "sess-1")

    assert result is True
    assert call_order == ["legacy", "session"], "Legacy must be written before session update"


@pytest.mark.asyncio
async def test_check_and_trim_legacy_fail_aborts(archive: MagicMock):
    """ACID: if legacy export fails, session must NOT be modified."""
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=True, keep_rounds=2, upper_limit_rounds=3)
    linear = _msgs(
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    )
    session = _make_session(linear)

    with (
        patch.object(svc, "load_config", return_value=cfg),
        patch("src.services.sliding_window_service.session_service") as mock_ss,
        patch("src.services.sliding_window_service.get_context_window", return_value=10),
    ):
        mock_ss.read_persistence_snapshot = AsyncMock(
            return_value=SessionSnapshot(session, "before")
        )
        mock_ss.conditional_save_session = AsyncMock(
            return_value=SavedResult(status="saved", representation="full", revision="after")
        )
        archive.side_effect = OSError("disk full")

        result = await svc.check_and_trim(DEBUG_IDENTITY, "sess-1")

    assert result is False
    mock_ss.conditional_save_session.assert_not_called()


@pytest.mark.asyncio
async def test_check_and_trim_session_fail_safe(archive: MagicMock):
    """ACID: if session update fails, legacy already saved; returns False but data preserved."""
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=True, keep_rounds=2, upper_limit_rounds=3)
    linear = _msgs(
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    )
    session = _make_session(linear)

    with (
        patch.object(svc, "load_config", return_value=cfg),
        patch("src.services.sliding_window_service.session_service") as mock_ss,
        patch("src.services.sliding_window_service.get_context_window", return_value=10),
    ):
        mock_ss.read_persistence_snapshot = AsyncMock(
            return_value=SessionSnapshot(session, "before")
        )
        mock_ss.conditional_save_session = AsyncMock(
            return_value=UnknownResult(status="unknown", error_code=1302)
        )  # DB outcome not confirmed

        result = await svc.check_and_trim(DEBUG_IDENTITY, "sess-1")

    assert result is False
    archive.assert_called_once()  # Legacy was archived (data preserved)


# ── compute_split_by_tokens (ADR-0092 Phase C) ──────────────────────────────


def _cfg_pct(**kw) -> SlidingWindowConfig:
    base = {"enabled": True, "trigger_pct": 90, "target_floor_pct": 50, "min_keep_pairs": 2}
    base.update(kw)
    return SlidingWindowConfig(**base)


def test_split_by_tokens_partition_and_anchor():
    svc = SlidingWindowService()
    msgs = _msgs(*(["user", "assistant"] * 6))  # 6 pairs
    legacy, kept = svc.compute_split_by_tokens(msgs, "", window=100, config=_cfg_pct())
    assert len(legacy) + len(kept) == len(msgs)
    assert legacy, "should trim something when over budget"
    assert kept[0]["parentId"] is None
    assert kept[0]["role"] == "user"  # 完整 pair 邊界


def test_split_by_tokens_keeps_min_pairs_when_overhead_huge():
    svc = SlidingWindowService()
    msgs = _msgs(*(["user", "assistant"] * 6))
    # 極小窗 → 預算極小 → 保底 min_keep_pairs。
    _, kept = svc.compute_split_by_tokens(msgs, "", window=10, config=_cfg_pct(min_keep_pairs=2))
    assert svc.count_rounds(kept) == 2


def test_split_by_tokens_nothing_to_trim_when_few_pairs():
    svc = SlidingWindowService()
    msgs = _msgs("user", "assistant", "user", "assistant")  # 2 pairs = min_keep_pairs
    legacy, kept = svc.compute_split_by_tokens(
        msgs, "", window=10, config=_cfg_pct(min_keep_pairs=2)
    )
    assert legacy == []
    assert len(kept) == len(msgs)


def test_split_by_tokens_authoritative_overhead_trims_more():
    svc = SlidingWindowService()
    msgs = _msgs(*(["user", "assistant"] * 6))
    cfg = _cfg_pct(target_floor_pct=80)
    # 高 current_total（overhead 大）→ 預算更小 → 保留更少。
    _, kept_low = svc.compute_split_by_tokens(msgs, "", 200, cfg, current_total=None)
    _, kept_high = svc.compute_split_by_tokens(msgs, "", 200, cfg, current_total=10000)
    assert len(kept_high) <= len(kept_low)


def test_split_by_tokens_exact_retained_pairs(monkeypatch):
    """釘死精確保留對數（先前測試只驗相對/保底，從未鎖定確切對數）。

    固定每則 10 tok（保留 k 對 = 後綴 20k tok）。窗 200 × 地板 30% → 預算 60 tok
    → 後綴 20×3=60 ≤ 60 成立、20×4=80 不成立 → 正好保留 3 對。
    殺死 `suffix_at[split_idx] <= budget` 的 `<=`→`<` 邊界與 keep_count 算術變異。
    """
    monkeypatch.setattr(
        "src.services.sliding_window_service.estimate_messages_tokens",
        lambda msgs, model: 10 * len(msgs),
    )
    svc = SlidingWindowService()
    msgs = _msgs(*(["user", "assistant"] * 6))  # 6 對 = 12 則
    cfg = _cfg_pct(target_floor_pct=30, min_keep_pairs=2)
    legacy, kept = svc.compute_split_by_tokens(msgs, "", window=200, config=cfg)
    assert svc.count_rounds(kept) == 3  # 精確 3 對（非僅 ">=2" 或相對）
    assert len(legacy) == 6  # 砍掉最舊 3 對
    assert kept[0]["parentId"] is None

    # 地板 40% → 預算 80 → 後綴 20×4=80 ≤ 80 成立 → 正好 4 對（釘死遞增步階）。
    cfg2 = _cfg_pct(target_floor_pct=40, min_keep_pairs=2)
    _, kept2 = svc.compute_split_by_tokens(msgs, "", window=200, config=cfg2)
    assert svc.count_rounds(kept2) == 4


# ── 換模型冷卻 ───────────────────────────────────────────────────────────────


def test_check_model_cooldown():
    svc = SlidingWindowService()
    assert svc._check_model_cooldown("s1", "claude") is False  # 首次：無冷卻
    assert svc._check_model_cooldown("s1", "claude") is False  # 同模型：無冷卻
    assert svc._check_model_cooldown("s1", "gemini") is True  # 換模型：冷卻
    assert svc._check_model_cooldown("s1", "gemini") is False  # 已穩定：恢復


# ── per-session 鎖（ADR-0092 Phase D / 禁令 57）───────────────────────────────


def test_get_lock_same_per_session_distinct_across():
    svc = SlidingWindowService()
    assert svc._get_lock("s1") is svc._get_lock("s1")  # 同 session 同鎖
    assert svc._get_lock("s1") is not svc._get_lock("s2")  # 不同 session 不同鎖


# ── pct 不變式 validator ─────────────────────────────────────────────────────


def test_pct_invariant_rejects_floor_plus_summary_ge_trigger():
    # Pydantic v2 將 model_validator 的 ValueError 包成 ValidationError（ValueError 子類）。
    with pytest.raises(ValueError, match="trigger_pct"):
        SlidingWindowConfig(
            enabled=True, trigger_pct=50, target_floor_pct=45, summary_budget_pct=10
        )


def test_pct_invariant_accepts_valid():
    cfg = SlidingWindowConfig(trigger_pct=95, target_floor_pct=45, summary_budget_pct=10)
    assert cfg.trigger_pct == 95


# ── 摘要整合（ADR-0092 Phase D）──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_and_trim_summarizes_and_writes_context_summary():
    """summary_budget_pct>0 → 摘要被呼叫，結果經 context_summary 寫入 session。"""
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(
        enabled=True, trigger_pct=90, target_floor_pct=50, summary_budget_pct=10
    )
    linear = _msgs(*(["user", "assistant"] * 4))
    session = _make_session(linear)

    with (
        patch.object(svc, "load_config", return_value=cfg),
        patch("src.services.sliding_window_service.session_service") as mock_ss,
        patch("src.services.sliding_window_service.get_context_window", return_value=10),
        patch(
            "src.services.sliding_window_service.summarize_dialogue",
            new=AsyncMock(return_value="摘要要點"),
        ) as mock_sum,
    ):
        mock_ss.read_persistence_snapshot = AsyncMock(
            return_value=SessionSnapshot(session, "before")
        )
        mock_ss.conditional_save_session = AsyncMock(
            return_value=SavedResult(status="saved", representation="full", revision="after")
        )

        result = await svc.check_and_trim(DEBUG_IDENTITY, "sess-1")

    assert result is True
    mock_sum.assert_awaited_once()
    assert mock_sum.await_args.args[0] is DEBUG_IDENTITY  # 摘要記帳到滑動視窗手上的身分
    args = mock_ss.conditional_save_session.await_args.args
    assert args[:2] == ("sess-1", "before")
    assert args[3] is DEBUG_IDENTITY
    assert args[2].context_summary == "摘要要點"
    assert "context_summary" in args[2].model_fields_set


@pytest.mark.asyncio
async def test_check_and_trim_no_summary_when_budget_zero():
    """summary_budget_pct=0（預設）→ 不呼叫摘要，context_summary 為 None（純無損剪裁）。"""
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(
        enabled=True, trigger_pct=90, target_floor_pct=50, summary_budget_pct=0
    )
    linear = _msgs(*(["user", "assistant"] * 4))
    session = _make_session(linear)

    with (
        patch.object(svc, "load_config", return_value=cfg),
        patch("src.services.sliding_window_service.session_service") as mock_ss,
        patch("src.services.sliding_window_service.get_context_window", return_value=10),
        patch(
            "src.services.sliding_window_service.summarize_dialogue",
            new=AsyncMock(return_value="不該被用"),
        ) as mock_sum,
    ):
        mock_ss.read_persistence_snapshot = AsyncMock(
            return_value=SessionSnapshot(session, "before")
        )
        mock_ss.conditional_save_session = AsyncMock(
            return_value=SavedResult(status="saved", representation="full", revision="after")
        )

        result = await svc.check_and_trim(DEBUG_IDENTITY, "sess-1")

    assert result is True
    mock_sum.assert_not_awaited()
    args = mock_ss.conditional_save_session.await_args.args
    assert args[:2] == ("sess-1", "before")
    assert args[3] is DEBUG_IDENTITY
    assert args[2].context_summary is None
    assert "context_summary" not in args[2].model_fields_set


# ── helper methods: _resolve_model / _current_total_tokens / _rebuild_tree /
#    _format_legacy_export (token-usage + tree + legacy-export helpers) ────────


def test_resolve_model_returns_most_recent_nonempty_model():
    """Iterates reversed → returns the model of the LAST message carrying a
    non-empty string model (not the first)."""
    linear = [
        {"id": "m0", "role": "user", "model": "claude"},
        {"id": "m1", "role": "assistant", "model": "gemini"},
        {"id": "m2", "role": "user"},  # no model → skipped
    ]
    assert SlidingWindowService._resolve_model(linear) == "gemini"


def test_resolve_model_empty_when_none_have_model():
    """No message carries a model → returns '' (empty string, not None)."""
    linear = _msgs("user", "assistant", "user")
    assert SlidingWindowService._resolve_model(linear) == ""


def test_resolve_model_skips_empty_string_and_non_dict_entries():
    """Empty-string model and non-dict entries are skipped; the earlier
    non-empty model wins."""
    linear = [
        {"id": "m0", "role": "user", "model": "claude"},
        {"id": "m1", "role": "assistant", "model": ""},  # empty → skipped
        "not-a-dict",  # non-dict → skipped
        {"id": "m2", "role": "user"},  # missing model → skipped
    ]
    assert SlidingWindowService._resolve_model(linear) == "claude"


def test_current_total_tokens_uses_authoritative_usage():
    """Most-recent authoritative progress.token_usage.total_tokens wins and is
    returned as int (estimate fallback must NOT be consulted)."""
    linear = [
        {
            "id": "m0",
            "role": "assistant",
            "progress": {"token_usage": {"total_tokens": 111}},
        },
        {
            "id": "m1",
            "role": "assistant",
            "progress": {"token_usage": {"total_tokens": 4242}},
        },
    ]
    with patch(
        "src.services.sliding_window_service.estimate_messages_tokens",
        return_value=999999,
    ) as mock_est:
        total = SlidingWindowService._current_total_tokens(linear, "claude")
    assert total == 4242
    mock_est.assert_not_called()


def test_current_total_tokens_falls_back_to_estimate():
    """No authoritative usage (total_tokens absent / not > 0) → falls back to
    estimate_messages_tokens(linear, model)."""
    linear = [
        {"id": "m0", "role": "user"},  # no progress
        {
            "id": "m1",
            "role": "assistant",
            "progress": {"token_usage": {"total_tokens": 0}},  # 0 → not authoritative
        },
    ]
    with patch(
        "src.services.sliding_window_service.estimate_messages_tokens",
        return_value=777,
    ) as mock_est:
        total = SlidingWindowService._current_total_tokens(linear, "claude")
    assert total == 777
    mock_est.assert_called_once_with(linear, "claude")


def test_rebuild_tree_empty_returns_empty_tree():
    """Empty messages → empty tree dict (matches create_empty_tree)."""
    assert SlidingWindowService._rebuild_tree([]) == {
        "messages": {},
        "currentId": None,
    }


def test_rebuild_tree_chains_parents_children_and_current_id():
    """Non-empty → keyed by id; parentId chain (first None, then previous id),
    childrenIds = [next id] except last = [], currentId = last id."""
    msgs = _msgs("user", "assistant", "user")  # ids m0, m1, m2
    tree = SlidingWindowService._rebuild_tree(msgs)

    assert set(tree.keys()) == {"messages", "currentId"}
    assert tree["currentId"] == "m2"
    assert set(tree["messages"].keys()) == {"m0", "m1", "m2"}

    # parentId chain: first None, each subsequent = previous id
    assert tree["messages"]["m0"]["parentId"] is None
    assert tree["messages"]["m1"]["parentId"] == "m0"
    assert tree["messages"]["m2"]["parentId"] == "m1"

    # childrenIds: [next id] except last = []
    assert tree["messages"]["m0"]["childrenIds"] == ["m1"]
    assert tree["messages"]["m1"]["childrenIds"] == ["m2"]
    assert tree["messages"]["m2"]["childrenIds"] == []


def test_format_legacy_export_structure():
    """One wrapper dict whose id/title/agent_id match inputs, exported_at is an
    int, and messages carry id/role/content/timestamp per source message."""
    msgs = [
        {"id": "m0", "role": "user", "content": "hi", "timestamp": 1000},
        {"id": "m1", "role": "assistant", "content": "yo", "timestamp": 2000},
    ]
    out = SlidingWindowService._format_legacy_export("sess-xyz", "My Title", msgs, DEBUG_IDENTITY)

    assert isinstance(out, list)
    assert len(out) == 1
    wrapper = out[0]
    assert wrapper["id"] == "sess-xyz"
    assert wrapper["title"] == "My Title"
    assert wrapper["agent_id"] == DEBUG_IDENTITY.agent_id  # "DebugAgent"
    assert isinstance(wrapper["exported_at"], int)  # time-based: type only

    exported = wrapper["messages"]
    assert isinstance(exported, list)
    assert exported == [
        {"id": "m0", "role": "user", "content": "hi", "timestamp": 1000},
        {"id": "m1", "role": "assistant", "content": "yo", "timestamp": 2000},
    ]


# ── 批⑤-D E49（ADR-0092）：per-session 鎖真併發序列化（禁令 57） ────────────────


def _concurrency_probe(svc: SlidingWindowService) -> dict:
    """把 _check_and_trim_inner 換成併發探針：記錄同時在臨界區內的最大數。"""
    state = {"now": 0, "max": 0}

    async def _slow_inner(identity: AgentIdentity, session_id: str) -> bool:
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.05)  # 撐開重疊視窗
        state["now"] -= 1
        return False

    svc._check_and_trim_inner = _slow_inner  # 實例層 shadow；鎖與入口皆走真實碼
    return state


@pytest.mark.asyncio
async def test_check_and_trim_serializes_same_session() -> None:
    """同 session 兩併發 check_and_trim 必序列化（heartbeat＋chat 雙觸發點競態）。

    既有唯一鎖測試只斷言 _get_lock 物件身分、從不真併發；變異移 :306 的
    `async with self._get_lock(...)` 直呼 inner → 臨界區重疊（max==2）→ 紅。
    """
    svc = SlidingWindowService()
    state = _concurrency_probe(svc)
    await asyncio.gather(
        svc.check_and_trim(DEBUG_IDENTITY, "sess-1"),
        svc.check_and_trim(DEBUG_IDENTITY, "sess-1"),
    )
    assert state["max"] == 1


@pytest.mark.asyncio
async def test_check_and_trim_parallel_across_sessions() -> None:
    """不同 session 不互擋（鎖是 per-session 非全域——防過度序列化回歸）。"""
    svc = SlidingWindowService()
    state = _concurrency_probe(svc)
    await asyncio.gather(
        svc.check_and_trim(DEBUG_IDENTITY, "sess-a"),
        svc.check_and_trim(DEBUG_IDENTITY, "sess-b"),
    )
    assert state["max"] == 2


# ── 剪下的對話直接歸檔進舊對話庫（legacy/ 受保護，不寫中繼 JSON 檔）──────────────


_SAVED = SavedResult(status="saved", representation="full", revision="after")
_UNKNOWN = UnknownResult(status="unknown", error_code=1302)


@pytest.mark.parametrize(
    ("archive_result", "save_result", "trimmed", "saved"),
    [
        (2, _SAVED, True, True),  # 歸檔成功 → 剪
        (2, _UNKNOWN, False, True),  # 存 session 結果不明 → 回 False；歸檔已在
        (OSError("database is locked"), _SAVED, False, False),  # 歸檔失敗 → 不剪、不摘要
        (0, _SAVED, False, False),  # 什麼都沒歸檔到 → 不剪（剪了會遺失）
    ],
)
@pytest.mark.asyncio
async def test_trim_archives_into_legacy_store_before_cutting(
    archive: MagicMock, archive_result, save_result, trimmed, saved
):
    svc = SlidingWindowService()
    cfg = SlidingWindowConfig(enabled=True, trigger_pct=90, target_floor_pct=50,
                              summary_budget_pct=10)
    session = _make_session(_msgs(*(["user", "assistant"] * 4)))
    if isinstance(archive_result, Exception):
        archive.side_effect = archive_result
    else:
        archive.return_value = archive_result
    with (
        patch.object(svc, "load_config", return_value=cfg),
        patch("src.services.sliding_window_service.session_service") as mock_ss,
        patch("src.services.sliding_window_service.get_context_window", return_value=10),
        patch("src.services.sliding_window_service.summarize_dialogue",
              new=AsyncMock(return_value="s")) as mock_sum,
    ):
        mock_ss.read_persistence_snapshot = AsyncMock(
            return_value=SessionSnapshot(session, "before")
        )
        mock_ss.conditional_save_session = AsyncMock(return_value=save_result)
        assert await svc.check_and_trim(DEBUG_IDENTITY, "sess-1") is trimmed

    identity, payload, label = archive.call_args.args
    assert identity is DEBUG_IDENTITY
    assert payload[0]["id"] == "sess-1" and payload[0]["agent_id"] == "DebugAgent"
    assert label.startswith("DebugAgent_sw_") and label.endswith(".json")
    assert mock_ss.conditional_save_session.await_count == (1 if saved else 0)
    assert mock_sum.await_count == (1 if saved else 0)


# ── 真的舊對話庫（tmp_path）：歸檔照實（舊審查報告乙-1） ─────────────────────────


@pytest.fixture
def real_archive(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """剪裁改用真的 LegacySyncService，舊對話庫寫在 tmp_path（不碰沙盒的舊對話庫）。"""
    memory = create_autospec(MemoryManager, instance=True)
    memory.get_legacy_dir.return_value = tmp_path
    service = LegacySyncService(memory)
    manager = MagicMock()
    manager.get_legacy_sync_service.return_value = service
    monkeypatch.setattr(sliding, "get_manager", lambda: manager)
    return tmp_path


def _legacy_rows(directory) -> list[tuple[str, str, dict]]:
    with contextlib.closing(_sqlite3.connect(directory / "legacy_history.db")) as conn:
        rows = conn.execute("SELECT id, content, metadata FROM messages").fetchall()
    return [(msg_id, content, json.loads(meta or "{}")) for msg_id, content, meta in rows]


async def _trim(svc: SlidingWindowService, session: dict, *results) -> list[bool]:
    cfg = SlidingWindowConfig(enabled=True, keep_rounds=2, upper_limit_rounds=3)
    outcomes = []
    with (
        patch.object(svc, "load_config", return_value=cfg),
        patch("src.services.sliding_window_service.session_service") as mock_ss,
        patch("src.services.sliding_window_service.get_context_window", return_value=10),
    ):
        mock_ss.read_persistence_snapshot = AsyncMock(
            return_value=SessionSnapshot(session, "before")
        )
        mock_ss.conditional_save_session = AsyncMock(side_effect=list(results))
        for _ in results:
            outcomes.append(await svc.check_and_trim(DEBUG_IDENTITY, "sess-1"))
    return outcomes


@pytest.mark.asyncio
async def test_a_retry_after_an_unknown_save_still_trims(real_archive) -> None:
    """存 session 結果不明後重剪：這批訊息已經在庫，仍算確認在庫，第二輪照常剪成.

    「回這次實際插入的筆數」的改法會讓第二輪拿到 0、永遠不剪；這裡釘住。
    """
    session = _make_session(_msgs(*(["user", "assistant"] * 4)))

    assert await _trim(SlidingWindowService(), session, _UNKNOWN, _SAVED) == [False, True]

    rows = _legacy_rows(real_archive)
    ids = [msg_id for msg_id, _, _ in rows]
    assert len(ids) == len(set(ids)) > 0
    assert not [meta for _, _, meta in rows if "version_of" in meta]


@pytest.mark.asyncio
async def test_an_archived_message_with_other_content_is_kept_as_a_version(real_archive) -> None:
    """庫裡已有同 id、不同內容的訊息：舊的不變，剪下的這則另存成版本，照常剪（乙-1 甲案）."""
    LegacyStore(str(real_archive / "legacy_history.db")).archive(
        DEBUG_IDENTITY,
        [{"id": "sess-1", "filename": "f", "title": "t", "created_at": 1, "updated_at": 1}],
        [{"id": "m0", "session_id": "sess-1", "role": "user", "content": "earlier text"}],
    )
    session = _make_session(_msgs(*(["user", "assistant"] * 4)))

    assert await _trim(SlidingWindowService(), session, _SAVED) == [True]

    rows = _legacy_rows(real_archive)
    assert ("m0", "earlier text") in [(msg_id, content) for msg_id, content, _ in rows]
    versions = [content for _, content, meta in rows if meta.get("version_of") == "m0"]
    assert versions == ["content_0"]
