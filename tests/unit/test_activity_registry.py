"""背景活動登記表 (activity_registry) 單元測試 — 背景 run 可觀察性的底層狀態。"""
from __future__ import annotations

from src.services.activity_registry import ActivityRegistry


def test_set_and_snapshot():
    reg = ActivityRegistry()
    reg.set_activity("s1", {"kind": "heartbeat", "current": 2, "total": 5})
    assert reg.snapshot() == {"s1": {"kind": "heartbeat", "current": 2, "total": 5}}


def test_set_overwrites():
    reg = ActivityRegistry()
    reg.set_activity("s1", {"current": 1})
    reg.set_activity("s1", {"current": 2})
    assert reg.snapshot()["s1"]["current"] == 2


def test_clear():
    reg = ActivityRegistry()
    reg.set_activity("s1", {"current": 1})
    reg.clear("s1")
    assert reg.snapshot() == {}


def test_set_ignores_empty_session():
    reg = ActivityRegistry()
    reg.set_activity(None, {"current": 1})
    reg.set_activity("", {"current": 1})
    assert reg.snapshot() == {}


def test_snapshot_is_copy():
    reg = ActivityRegistry()
    reg.set_activity("s1", {"current": 1})
    snap = reg.snapshot()
    snap["s2"] = {"current": 9}
    assert "s2" not in reg.snapshot()  # 快照不影響內部狀態


def test_reload_all():
    reg = ActivityRegistry()
    reg.set_activity("s1", {"current": 1})
    reg.reload_all()
    assert reg.snapshot() == {}
