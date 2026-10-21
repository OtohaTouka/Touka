"""P5a 原子寫入 ＋ 壞檔可見 — resource_manager 資料完整性。"""
from __future__ import annotations

import pytest
from src.services.resource_manager import resource_manager


def _raise(*_a, **_k):
    raise RuntimeError("simulated write failure")


def test_write_file_text_atomic_success(tmp_path):
    f = tmp_path / "data.txt"
    resource_manager.write_file_text(f, "hello-atomic")
    assert f.read_text(encoding="utf-8") == "hello-atomic"
    # 成功後無殘留暫存檔
    assert not list(tmp_path.glob(".*.tmp"))


def test_write_file_text_failure_preserves_original(tmp_path, monkeypatch):
    f = tmp_path / "data.txt"
    f.write_text("ORIGINAL", encoding="utf-8")
    # 模擬置換階段失敗 → 原子性應保住原檔、清掉暫存
    monkeypatch.setattr("src.services.resource_manager.os.replace", _raise)
    with pytest.raises(RuntimeError):
        resource_manager.write_file_text(f, "NEW")
    assert f.read_text(encoding="utf-8") == "ORIGINAL"
    assert not list(tmp_path.glob(".*.tmp"))


def test_write_json_is_atomic_roundtrip(tmp_path):
    f = tmp_path / "data.json"
    resource_manager.write_json(f, {"a": 1, "b": [2, 3]})
    assert resource_manager.read_json(f) == {"a": 1, "b": [2, 3]}
    assert not list(tmp_path.glob(".*.tmp"))


def test_read_json_corrupt_returns_none(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not valid json", encoding="utf-8")
    assert resource_manager.read_json(f) is None
