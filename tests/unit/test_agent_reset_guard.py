"""軌B Batch 6：DebugAgent 重置安全閘——硬鎖只允許 DebugAgent、絕不誤刪真實 agent（零-API）。"""
from __future__ import annotations

import pytest

from src.core.exceptions import ToukaException
from src.services.agent_reset.guard import assert_resettable
from src.services.agent_service import agent_service
from src.utils import soul_io


def test_debugagent_allowed():
    assert assert_resettable("DebugAgent") == "DebugAgent"


def test_legacy_toukasoul_prefix_normalized():
    # 正規化去 ToukaSoul 前綴後仍是 DebugAgent → 放行。
    assert assert_resettable("ToukaSoulDebugAgent") == "DebugAgent"


@pytest.mark.parametrize(
    "bad",
    ["Jin", "Yan", "Code", "Alice", "", "  ", "debugagent", "DebugAgent2", "Guest", "ToukaSoulJin"],
)
def test_non_debugagent_rejected(bad):
    with pytest.raises(ToukaException):
        assert_resettable(bad)


def test_belt_rejects_when_debugagent_has_soul_dir(monkeypatch):
    # 即使硬鎖通過，若 DebugAgent 竟有實體 soul 目錄（身分異常）→ 第二道防線拒絕。
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: ["DebugAgent", "Jin"])
    with pytest.raises(ToukaException):
        assert_resettable("DebugAgent")


def test_belt_rejects_when_debugagent_has_custom_persona(monkeypatch):
    # 真實 custom 人格＝identify_agent 回 is_custom（有 config.json）；非僅憑 custom 目錄存在。
    monkeypatch.setattr(agent_service, "identify_agent", lambda _c: {"is_custom": True})
    with pytest.raises(ToukaException):
        assert_resettable("DebugAgent")


def test_listing_failure_is_conservative(monkeypatch):
    # 列舉失敗 → 無法證明「非真實 agent」→ 保守拒絕（安全優先）。
    def _boom():
        raise RuntimeError("disk error")

    monkeypatch.setattr(soul_io, "list_soul_ids", _boom)
    with pytest.raises(ToukaException):
        assert_resettable("DebugAgent")
