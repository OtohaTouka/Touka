"""Unit tests for verifying system error configurations."""

import json
from pathlib import Path

import pytest


def test_errors_json_ids_are_unique():
    """驗證 tracked 出貨範本的全部錯誤碼 id 唯一，不讀 runtime 覆寫。"""
    errors_json_path = (
        Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "errors.json.example"
    )
    assert errors_json_path.exists(), f"errors.json not found at {errors_json_path}"

    with errors_json_path.open(encoding="utf-8") as f:
        errors = json.load(f)

    ids = []
    duplicates = {}
    for key, val in errors.items():
        err_id = val.get("id")
        if err_id in ids:
            duplicates[err_id] = duplicates.get(err_id, [])
            if key not in duplicates[err_id]:
                duplicates[err_id].append(key)
        else:
            ids.append(err_id)

    # 找出與當前 err_id 重複的其他 key
    for err_id, keys in duplicates.items():
        # 把原本第一次出現的 key 也找出來
        first_key = next(k for k, v in errors.items() if v.get("id") == err_id and k not in keys)
        keys.insert(0, first_key)

    assert not duplicates, f"偵測到重複的錯誤碼 ID (SSOT 衝突)！\n重複詳情：{duplicates}"


def test_duplicate_error_ids_still_fail(monkeypatch):
    """Changing the input to the shipped template must retain the duplicate check."""
    monkeypatch.setattr(json, "load", lambda _stream: {"FIRST": {"id": 7}, "SECOND": {"id": 7}})
    with pytest.raises(AssertionError, match="重複的錯誤碼 ID"):
        test_errors_json_ids_are_unique()
