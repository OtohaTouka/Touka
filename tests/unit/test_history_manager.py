"""Unit tests for TreeMessageManager.get_linear_path 環防護（cycle guard）。

先前 history_manager 的 `get_linear_path` 雖已有 `visited` 環防護，卻無測試覆蓋
→ 拿掉 `not in visited` / `visited.add` 的變異存活；而 parentId 一旦成環
（資料損毀 / 惡意輸入）就會無限迴圈掛死（DoS）。本檔釘死那道防線。
"""

from src.utils.history_manager import (
    TreeMessageManager,
    append_message_to_history,
    count_rounds_from_linear,
    sanitize_history,
)


def test_get_linear_path_breaks_on_parentid_cycle():
    """parentId 形成環時必須終止，且每節點最多取一次（殺死環防護變異）。"""
    tree = {
        "messages": {
            "a": {"id": "a", "role": "user", "content": "A", "parentId": "b"},
            "b": {"id": "b", "role": "assistant", "content": "B", "parentId": "a"},
        },
        "currentId": "a",
    }
    # 有環防護才會終止；若防護被移除，此輸入會無限迴圈（mutmut 以逾時計為 killed）。
    path = TreeMessageManager.get_linear_path(tree)
    ids = [m["id"] for m in path]
    assert len(ids) == len(set(ids))  # 無重複節點
    assert set(ids) <= {"a", "b"}


def test_get_linear_path_self_loop_terminates():
    """節點 parentId 指向自己（self-loop）也必須終止，只回傳該節點一次。"""
    tree = {
        "messages": {
            "x": {"id": "x", "role": "user", "content": "X", "parentId": "x"},
        },
        "currentId": "x",
    }
    path = TreeMessageManager.get_linear_path(tree)
    assert [m["id"] for m in path] == ["x"]


def test_get_linear_path_acyclic_unaffected():
    """非環的正常鏈不受環防護影響，完整還原線性順序（根 → 葉）。"""
    tree = {
        "messages": {
            "a": {"id": "a", "role": "user", "content": "A", "parentId": None},
            "b": {"id": "b", "role": "assistant", "content": "B", "parentId": "a"},
            "c": {"id": "c", "role": "user", "content": "C", "parentId": "b"},
        },
        "currentId": "c",
    }
    assert [m["id"] for m in TreeMessageManager.get_linear_path(tree)] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# get_linear_path 邊界分支（list 直通、空輸入、缺 currentId、非 dict）
# ---------------------------------------------------------------------------


def test_get_linear_path_list_returned_as_is():
    """已是列表格式的輸入應原封不動回傳同一物件。"""
    lst = [{"id": "a"}, {"id": "b"}]
    assert TreeMessageManager.get_linear_path(lst) is lst


def test_get_linear_path_none_returns_empty():
    """None 輸入回傳空列表。"""
    assert TreeMessageManager.get_linear_path(None) == []


def test_get_linear_path_empty_dict_returns_empty():
    """空字典（falsy）回傳空列表。"""
    assert TreeMessageManager.get_linear_path({}) == []


def test_get_linear_path_dict_without_currentid_returns_empty():
    """有 messages 但 currentId 為 None（缺指針）回傳空列表。"""
    tree = {"messages": {"a": {"id": "a", "parentId": None}}, "currentId": None}
    assert TreeMessageManager.get_linear_path(tree) == []


def test_get_linear_path_non_dict_non_list_returns_empty():
    """非 dict 非 list（如 int）的 truthy 輸入回傳空列表。"""
    assert TreeMessageManager.get_linear_path(123) == []


# ---------------------------------------------------------------------------
# TreeMessageManager.append_node — id/parent/name/currentId 語意
# ---------------------------------------------------------------------------


def test_append_node_first_node_parent_defaults_to_none_and_stored():
    """空樹上第一個節點：parentId 預設為 currentId(None)，且被存入 messages。"""
    tree = TreeMessageManager.create_empty_tree()
    out = TreeMessageManager.append_node(tree, "user", "hi")
    new_id = out["currentId"]
    assert len(out["messages"]) == 1
    node = out["messages"][new_id]
    assert node["id"] == new_id
    assert node["role"] == "user"
    assert node["content"] == "hi"
    assert node["parentId"] is None


def test_append_node_parent_defaults_to_current_id():
    """未指定 parent_id 時，新節點的 parentId 接在前一個 currentId 後面。"""
    tree = TreeMessageManager.create_empty_tree()
    first = TreeMessageManager.append_node(tree, "user", "a")
    first_id = first["currentId"]
    second = TreeMessageManager.append_node(first, "assistant", "b")
    second_id = second["currentId"]
    assert second["messages"][second_id]["parentId"] == first_id
    assert second_id != first_id


def test_append_node_explicit_parent_id_honored():
    """顯式指定 parent_id 時應被採用，覆蓋預設的 currentId 邏輯。"""
    tree = TreeMessageManager.create_empty_tree()
    base = TreeMessageManager.append_node(tree, "user", "a")
    out = TreeMessageManager.append_node(base, "user", "b", parent_id="custom-parent")
    assert out["messages"][out["currentId"]]["parentId"] == "custom-parent"


def test_append_node_name_added_only_when_truthy():
    """name 為真值時加入 'name' 鍵；為空字串時不加入該鍵。"""
    tree = TreeMessageManager.create_empty_tree()
    named = TreeMessageManager.append_node(tree, "assistant", "x", name="Agent1")
    assert named["messages"][named["currentId"]]["name"] == "Agent1"

    unnamed = TreeMessageManager.append_node(named, "assistant", "y", name="")
    assert "name" not in unnamed["messages"][unnamed["currentId"]]


def test_append_node_updates_current_id_to_new_node():
    """currentId 必須更新為新節點的 id。"""
    tree = TreeMessageManager.create_empty_tree()
    assert tree["currentId"] is None
    out = TreeMessageManager.append_node(tree, "user", "hi")
    new_id = out["currentId"]
    assert new_id is not None
    assert new_id in out["messages"]


def test_append_node_invalid_tree_creates_empty_tree_first():
    """傳入空/無效樹（缺 messages）時先建立空樹，再加入單一節點。"""
    out = TreeMessageManager.append_node({}, "user", "x")
    assert set(out.keys()) == {"messages", "currentId"}
    assert len(out["messages"]) == 1
    assert out["currentId"] in out["messages"]


# ---------------------------------------------------------------------------
# count_rounds_from_linear — 精確來回計數
# ---------------------------------------------------------------------------


def test_count_rounds_two_complete_pairs():
    """兩組完整 user+assistant 配對 = 2 rounds。"""
    msgs = [
        {"role": "user"},
        {"role": "assistant"},
        {"role": "user"},
        {"role": "assistant"},
    ]
    assert count_rounds_from_linear(msgs) == 2


def test_count_rounds_trailing_user_not_counted():
    """末尾沒有 assistant 配對的 user 不計入。"""
    msgs = [
        {"role": "user"},
        {"role": "assistant"},
        {"role": "user"},
    ]
    assert count_rounds_from_linear(msgs) == 1


def test_count_rounds_skips_intervening_system_nudge():
    """user 與 assistant 之間的 system/nudge 角色被跳過，仍計入該 round。"""
    msgs = [
        {"role": "user"},
        {"role": "system"},
        {"role": "assistant"},
        {"role": "user"},
        {"role": "nudge"},
        {"role": "assistant"},
    ]
    assert count_rounds_from_linear(msgs) == 2


def test_count_rounds_empty_list_is_zero():
    """空列表 = 0 rounds。"""
    assert count_rounds_from_linear([]) == 0


# ---------------------------------------------------------------------------
# sanitize_history — 工具/幻覺清理、user 不動、in-place 修改
# ---------------------------------------------------------------------------


def test_sanitize_removes_tool_execution_line_from_assistant():
    """assistant 內容中的「工具執行」行被移除（原地修改 dict）。"""
    msg = {"role": "assistant", "content": "Hello\n> 正在執行工具：search ✅ done\nWorld"}
    sanitize_history([msg])
    assert msg["content"] == "Hello\n\nWorld"


def test_sanitize_removes_hallucination_warning_from_assistant():
    """assistant 內容中的「幻覺警告」行被移除並 strip。"""
    msg = {"role": "assistant", "content": "Real answer\n⚠️ [工具幻覺警告] I used a tool"}
    sanitize_history([msg])
    assert msg["content"] == "Real answer"


def test_sanitize_leaves_user_content_untouched():
    """同樣含可清理樣式的 user 訊息不被觸碰。"""
    original = "Hello\n> 正在執行工具：search ✅ done\nWorld"
    msg = {"role": "user", "content": original}
    sanitize_history([msg])
    assert msg["content"] == original


def test_sanitize_no_change_when_no_pattern_matches():
    """沒有任何樣式命中時，assistant 內容不被改動。"""
    msg = {"role": "assistant", "content": "Just normal text"}
    sanitize_history([msg])
    assert msg["content"] == "Just normal text"


def test_sanitize_strips_after_removal():
    """清理後對結果 strip，去除前後空白。"""
    msg = {"role": "assistant", "content": "  Answer\n⚠️ [記憶幻覺警告] bad  "}
    sanitize_history([msg])
    assert msg["content"] == "Answer"


# ---------------------------------------------------------------------------
# append_message_to_history — dict / list / 其他輸入路由
# ---------------------------------------------------------------------------


def test_append_message_to_history_dict_routes_through_append_node():
    """dict-with-'messages' 走 append_node：currentId 被設定且訊息入樹。"""
    tree = TreeMessageManager.create_empty_tree()
    out = append_message_to_history(tree, "user", "hello")
    assert out["currentId"] is not None
    node = out["messages"][out["currentId"]]
    assert node["role"] == "user"
    assert node["content"] == "hello"


def test_append_message_to_history_list_appends_dict():
    """list 輸入：附加一個含 role/content 的 dict，回傳同一個 list。"""
    history = [{"role": "user", "content": "a"}]
    out = append_message_to_history(history, "assistant", "b")
    assert out is history
    assert len(out) == 2
    assert out[-1]["role"] == "assistant"
    assert out[-1]["content"] == "b"


def test_append_message_to_history_other_input_returns_new_single_element_list():
    """其他輸入（如 None）回傳新的單元素 list。"""
    out = append_message_to_history(None, "user", "c")
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "c"
