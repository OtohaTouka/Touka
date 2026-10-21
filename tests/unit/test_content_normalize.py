"""多模態內容正規化 SSOT（Phase 3）：把 ToukaChat 夾帶的 JSON 字串還原為 list。

純函式測試，不觸碰任何 agent 資料/磁碟。
"""
from __future__ import annotations

import json

import pytest
from src.adapters.content_normalize import append_text_part, normalize_message_content

_IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR=="}}
_TXT = {"type": "text", "text": "hi"}


def test_parses_stringified_multimodal_array():
    out = normalize_message_content(json.dumps([_TXT, _IMG]))
    assert isinstance(out, list)
    assert [b["type"] for b in out] == ["text", "image_url"]


def test_plain_string_passes_through():
    assert normalize_message_content("hello world") == "hello world"


def test_already_a_list_passes_through():
    blocks = [_TXT, _IMG]
    assert normalize_message_content(blocks) is blocks


def test_none_passes_through():
    assert normalize_message_content(None) is None


def test_non_block_json_array_is_not_misparsed():
    # 使用者純文字剛好是 JSON 陣列但元素非「含 type 的 dict」→ 不還原（防誤判）。
    for text in ("[1, 2, 3]", '["a", "b"]', '[{"foo": 1}]', "[]"):
        assert normalize_message_content(text) == text


def test_malformed_json_passes_through():
    assert normalize_message_content("[not valid json") == "[not valid json"


def test_array_with_surrounding_whitespace():
    out = normalize_message_content("  " + json.dumps([_TXT]) + "\n")
    assert isinstance(out, list) and out[0]["type"] == "text"


def test_typed_dict_array_with_unknown_type_preserved_as_text():
    # 複審：使用者貼的 typed-dict JSON（type 非多模態）→ 原樣保留為文字，不還原、不丟失。
    for text in (
        '[{"type": "foo"}]',
        '[{"type": "input_text", "text": "hi"}]',
        '[{"type": "Point", "coordinates": [1, 2]}]',
        json.dumps([_TXT, {"type": "bar"}]),  # 混入一個未知型別 → 全段保留
    ):
        assert normalize_message_content(text) == text


def test_recognized_block_array_still_normalized():
    out = normalize_message_content(json.dumps([_TXT, _IMG]))
    assert isinstance(out, list) and [b["type"] for b in out] == ["text", "image_url"]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("hi", "hi+note"),  # 純文字：直接接
        ('[{"foo": 1}]', '[{"foo": 1}]+note'),  # 看起來像 JSON 但不是多模態：當文字
        (json.dumps([_TXT, _IMG]), [_TXT, _IMG, {"type": "text", "text": "+note"}]),
        ([_TXT, _IMG], [_TXT, _IMG, {"type": "text", "text": "+note"}]),
        (None, None),  # 不認得的形狀：不附加
        (42, None),
    ],
)
def test_append_text_part(content, expected):
    """尾端附一段文字（continuity 與 IDE 區塊共用）：多模態先還原成 list、另加一段，不改原 list。"""
    original = list(content) if isinstance(content, list) else content
    assert append_text_part(content, "+note") == expected
    if isinstance(content, list):
        assert content == original
