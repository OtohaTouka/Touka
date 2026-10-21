"""漂移修復批 C2：記憶/誠實訊號結構化（D3＋D4）。

根因：記憶工具大一統後對外只剩 `memory_tools(action=save/query/...)`，但
guard_service 仍以已退役的工具名（save_memory/query_memory/access_personal_drive）
比對 `actually_used_tools` → openai/gemini 的記憶幻覺檢查誤報恆真、誠實度檢查
恆不知查證、e_index 檢索分量恆 0（string-sniff 不存在的名字）；claude 以
handler 私有 block-walking 旗標另行實作＝三份拷貝的漂移。

修法（訊號生產單點＝SSOT）：tool_executor 於記名時對帶 `action` 參數的聚合工具
同時記入限定名 `name.action`（如 memory_tools.save）；guard_service 判定與
e_index 檢索訊號一律改讀該 SSOT；九裸動詞（read/list/find/grep…）無 action、
依工具名本身計（其內部委派 vault_tools 為 Python 呼叫、不經 tool_executor，
故查證集顯式列裸動詞）。
"""

from __future__ import annotations

import inspect

import pytest
from src.handlers import react_core
from src.services.guard_service import guard_service
from src.services.tool_executor import record_tool_usage

# react-core-extraction E4：三 handler 的 e_index 訊號站已單源於 react_core
# （薄殼零邏輯由 test_react_core_ssot.py 守衛錨把關）。
_HANDLER_MODULES = {
    "react_core.py": react_core,
}

_PROMISE_TEXT = "好的，我幫你記下來了！"
_TRIGGER_TEXT = "根據過去的記憶系統內容，你上次說喜歡紅茶。"


def _src(name: str) -> str:
    # inspect.getsource（非檔案 I/O）＝靜態源碼確認的正統路徑
    return inspect.getsource(_HANDLER_MODULES[name])


# ── 訊號生產（tool_executor 單點）──────────────────────────────────────────


def test_record_tool_usage_adds_name_and_qualified_action():
    used: set[str] = set()
    record_tool_usage(used, "memory_tools", {"action": "save", "content": "x"})
    assert used == {"memory_tools", "memory_tools.save"}


def test_record_tool_usage_bare_verb_has_no_action():
    used: set[str] = set()
    record_tool_usage(used, "read", {"scope": "personal", "path": "a.md"})
    assert used == {"read"}


def test_record_tool_usage_none_tracking_is_noop():
    record_tool_usage(None, "memory_tools", {"action": "save"})  # 不得拋錯


def test_record_tool_usage_non_string_action_ignored():
    used: set[str] = set()
    record_tool_usage(used, "weird_tool", {"action": 123})
    assert used == {"weird_tool"}


# ── guard_service 判定改讀 SSOT ────────────────────────────────────────────


def test_memory_promise_suppressed_by_qualified_save():
    assert (
        guard_service.check_memory_hallucination(_PROMISE_TEXT, {"memory_tools.save"})
        is False
    )


def test_memory_promise_without_save_still_warns():
    assert guard_service.check_memory_hallucination(_PROMISE_TEXT, set()) is True


@pytest.mark.parametrize(
    "used",
    [
        {"memory_tools.query"},   # 記憶查詢
        {"read"},                 # 裸動詞 read-family＝檔案查證
        {"grep"},
        {"vault_tools.read"},     # shim 相容
        {"vault_tools.grep"},
    ],
)
def test_fact_check_suppressed_by_verification_tools(used: set[str]):
    assert guard_service.check_fact_without_tools(_TRIGGER_TEXT, used) is False


def test_fact_check_without_verification_still_warns():
    assert guard_service.check_fact_without_tools(_TRIGGER_TEXT, set()) is True


def test_write_side_tools_do_not_count_as_verification():
    """寫入面（write/delete/vault_tools.write）不算查證。"""
    assert guard_service.check_fact_without_tools(_TRIGGER_TEXT, {"write"}) is True
    assert (
        guard_service.check_fact_without_tools(_TRIGGER_TEXT, {"vault_tools.write"})
        is True
    )


def test_warning_messages_reference_only_real_tools():
    """警告文案不得教已退役的工具名（防幻覺系統不能自己教幻覺）。

    ADR-0157：矯正提示（上游 prompt 面）納入同一鐵則——舊 gemini 內嵌版
    教 query_memory/access_personal_drive＝最後一個漏網站點。
    """
    for msg in (
        guard_service.get_memory_warning_message(),
        guard_service.get_honesty_warning_message(),
        guard_service.get_honesty_correction_prompt(),
    ):
        assert "save_memory" not in msg
        assert "query_memory" not in msg
        assert "access_personal_drive" not in msg
    assert "memory_tools" in guard_service.get_memory_warning_message()
    assert "memory_tools" in guard_service.get_honesty_warning_message()
    assert "memory_tools" in guard_service.get_honesty_correction_prompt()


# ── handler 接線（靜態確認，比照禁令 26 範式）─────────────────────────────


@pytest.mark.parametrize("handler_file", ["react_core.py"])
def test_e_index_retrieval_signal_reads_ssot(handler_file: str):
    src = _src(handler_file)
    assert '"memory_tools.query" in actually_used_tools' in src
    assert '"query_memory" in all_thinking_and_tool_logs' not in src


def test_claude_private_memory_flags_retired():
    """claude 的 block-walking 旗標（memory_saved/memory_queried）已退役＝訊號單源。"""
    src = _src("react_core.py")
    assert "memory_saved" not in src
    assert "memory_queried" not in src
