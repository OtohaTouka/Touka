"""測試 TreeHistory Pydantic 模型的 JSON 序列化相容性.

確保 TreeHistory 物件經 model_dump() 後可被 json.dumps() 處理。
"""

import json

import pytest
from src.models.history import TreeHistory
from src.routers.ui_sessions import SessionModel


def test_tree_history_serializable():
    """TreeHistory 物件經 model_dump() 後應可被 json.dumps 正確序列化。"""
    tree = TreeHistory(messages={}, currentId=None)
    dumped = tree.model_dump()
    # 這不應該拋出 TypeError
    result = json.dumps(dumped, ensure_ascii=False)
    assert '"messages"' in result


def test_tree_history_with_data_serializable():
    """含有節點資料的 TreeHistory 也應可序列化。"""
    tree = TreeHistory(
        messages={
            "node-1": {
                "id": "node-1",
                "role": "user",
                "content": "Hello",
                "parentId": None,
            }
        },
        currentId="node-1",
    )
    dumped = tree.model_dump()
    result = json.dumps(dumped, ensure_ascii=False)
    assert "Hello" in result


def test_raw_tree_history_fails_serialization():
    """未經 model_dump() 的 TreeHistory 物件不應可被 json.dumps 直接處理。"""
    tree = TreeHistory(messages={}, currentId=None)
    with pytest.raises(TypeError):
        json.dumps(tree, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 批⑤-E E25（ADR-0042）：from_flat_list 遷移邏輯——既有測試 payload 全空 list
# --------------------------------------------------------------------------- #


def test_from_flat_list_builds_parent_chain_children_and_current():
    """非空扁平列表 → parentId 鏈／childrenIds／currentId 全正確。

    變異刪 childrenIds append（history.py:75）→ children 斷言紅。
    """
    flat = [
        {"id": "a", "role": "user", "content": "1"},
        {"id": "b", "role": "assistant", "content": "2"},
        {"id": "c", "role": "user", "content": "3"},
    ]
    tree = TreeHistory.from_flat_list(flat)

    assert tree.currentId == "c"
    assert tree.messages["a"].parentId is None
    assert tree.messages["b"].parentId == "a"
    assert tree.messages["c"].parentId == "b"
    assert tree.messages["a"].childrenIds == ["b"]
    assert tree.messages["b"].childrenIds == ["c"]
    assert tree.messages["c"].childrenIds == []


def test_from_flat_list_generates_legacy_ids_when_missing():
    """缺 id 的舊訊息 → msg_legacy_{i} 補位且鏈結不斷。"""
    flat = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    tree = TreeHistory.from_flat_list(flat)

    assert set(tree.messages) == {"msg_legacy_0", "msg_legacy_1"}
    assert tree.messages["msg_legacy_1"].parentId == "msg_legacy_0"
    assert tree.currentId == "msg_legacy_1"


def test_session_model_validator_migrates_flat_list():
    """消費端接線（ui_sessions SessionModel.ensure_tree_format）：扁平 list 進模型
    → 依 TreeHistory 遷移為公開 JSON 樹（刪 validator → messages 仍是 list → 紅）。"""
    m = SessionModel(
        id="s1",
        title="t",
        messages=[
            {"id": "a", "role": "user", "content": "1"},
            {"id": "b", "role": "assistant", "content": "2"},
        ],
        updatedAt=1,
    )
    assert isinstance(m.messages, dict)
    assert m.messages["currentId"] == "b"
    assert m.messages["messages"]["b"]["parentId"] == "a"
    assert m.model_dump()["messages"] == m.messages
