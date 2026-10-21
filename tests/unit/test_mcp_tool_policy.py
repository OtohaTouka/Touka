"""MCP 工具面治理 SSOT 純函式測試（ADR-0195）。

釘死兩軸的比對語意，特別是三個容易寫錯、且錯了會靜默的判別：
1. ``enabled_tools`` 的 ``None``（未設＝全開）與 ``[]``（明示全關）**語意不同**——
   合併成同一個會讓「使用者把工具全部取消勾選」變成「全部開放」。
2. ``disabled_tools`` **恆優先**於 ``enabled_tools``（deny wins）。
3. 軸 B 的裸伺服器名條目（``"stripe"``）與單一工具條目（``"stripe/create_refund"``）
   命中範圍不同；``server_of_ref`` 對全域萬用符必須回 ``None`` 而非字串 ``"*"``。
"""
from __future__ import annotations

import pytest
from src.utils.mcp_tool_policy import (
    is_tool_enabled,
    matches_tool_ref,
    server_of_ref,
    validate_tool_ref,
)

# --- 軸 A：per-installation ---


def test_unset_enabled_tools_means_all_open():
    """``None`` ＝未設定＝全開——既有條目升級後不得整批消失。"""
    assert is_tool_enabled("create_refund") is True
    assert is_tool_enabled("create_refund", None, []) is True


def test_empty_enabled_tools_means_all_closed():
    """``[]`` ＝使用者明示全部取消勾選＝全關（與 ``None`` 語意相反，不可合併）。"""
    assert is_tool_enabled("create_refund", []) is False


def test_enabled_tools_is_an_allowlist():
    assert is_tool_enabled("list_customers", ["list_customers"]) is True
    assert is_tool_enabled("create_refund", ["list_customers"]) is False


def test_wildcard_in_enabled_tools_opens_all():
    assert is_tool_enabled("create_refund", ["*"]) is True


def test_disabled_tools_beats_enabled_tools():
    """deny wins：同時列名時一律拒絕——白名單不得反轉黑名單。"""
    assert is_tool_enabled("create_refund", ["create_refund"], ["create_refund"]) is False
    assert is_tool_enabled("create_refund", ["*"], ["create_refund"]) is False


def test_disabled_wildcard_closes_all_even_when_enabled_unset():
    assert is_tool_enabled("anything", None, ["*"]) is False


def test_no_prefix_glob_support():
    """刻意不支援前綴 glob——上游新增工具不得靜默落進既有樣式。"""
    assert is_tool_enabled("stripe_api_write", ["stripe_api_*"]) is False


# --- 軸 B：per-role 的 server/tool 條目 ---


@pytest.mark.parametrize(
    ("pattern", "server", "tool", "expected"),
    [
        ("*", "stripe", "create_refund", True),
        ("stripe", "stripe", "create_refund", True),
        ("stripe", "stripe", "list_customers", True),
        ("stripe/*", "stripe", "create_refund", True),
        ("stripe/create_refund", "stripe", "create_refund", True),
        ("stripe/create_refund", "stripe", "list_customers", False),
        ("stripe/create_refund", "github", "create_refund", False),
        ("stripe", "github", "create_refund", False),
    ],
)
def test_matches_tool_ref(pattern, server, tool, expected):
    assert matches_tool_ref(pattern, server, tool) is expected


def test_server_of_ref_returns_none_for_global_wildcard():
    """``"*"`` 不指向任何特定伺服器——回字串 ``"*"`` 會讓「掛不掛這台」的比對誤命中。"""
    assert server_of_ref("*") is None


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [("stripe", "stripe"), ("stripe/create_refund", "stripe"), ("stripe/*", "stripe")],
)
def test_server_of_ref_extracts_server_part(pattern, expected):
    assert server_of_ref(pattern) == expected


# --- 條目形狀驗證（Guard #32 的 SSOT）---

_LEGAL_ALLOW_REFS = ["*", "stripe", "stripe/*", "stripe/create_refund"]
_DEAD_REFS = ["", " ", " stripe", "stripe ", "/tool", "stripe/", "a//b", "a/b/c", "*/tool"]


@pytest.mark.parametrize("ref", _LEGAL_ALLOW_REFS)
def test_validate_accepts_legal_allow_shapes(ref):
    assert validate_tool_ref(ref, allow_bare_wildcard=True) is None


@pytest.mark.parametrize("ref", _DEAD_REFS)
def test_validate_rejects_dead_shapes(ref):
    assert validate_tool_ref(ref, allow_bare_wildcard=True) is not None


def test_validate_rejects_bare_wildcard_on_deny_side():
    assert validate_tool_ref("*", allow_bare_wildcard=False) is not None
    assert validate_tool_ref("stripe/*", allow_bare_wildcard=False) is None


@pytest.mark.parametrize("ref", [1, None, ["stripe"], {"a": 1}])
def test_validate_rejects_non_strings(ref):
    assert validate_tool_ref(ref, allow_bare_wildcard=True) is not None


@pytest.mark.parametrize("ref", _DEAD_REFS)
def test_rejected_shapes_never_match_realistic_names(ref):
    """**驗證器與比對器必須同進同退**：被拒的形狀對任何**真實可能出現的**名稱都不匹配。

    這是「拒絕它」的正當性依據——若某個被拒形狀其實匹配得到真實名稱，守衛就是在擋合法設定。

    誠實界定：「真實可能出現」＝伺服器鍵與工具名皆非空、無空白、不含斜線。退化名稱
    （如伺服器literally 叫 `""` 或工具名含 `/`）確實能讓部分死字串匹配，但那種名稱
    `derive_server_key` 產不出來、MCP 伺服器也不會回報。刻意不宣稱「永不匹配任何輸入」。
    """
    realistic = [
        ("stripe", "create_refund"),
        ("stripe", "list_customers"),
        ("github", "list_issues"),
        ("mcp-docgen", "generate"),
        ("a", "b"),
    ]
    assert not any(matches_tool_ref(ref, s, t) for s, t in realistic)


@pytest.mark.parametrize(
    ("ref", "server", "tool"),
    [
        ("*", "stripe", "create_refund"),
        ("stripe", "stripe", "create_refund"),
        ("stripe/*", "stripe", "create_refund"),
        ("stripe/create_refund", "stripe", "create_refund"),
    ],
)
def test_accepted_shapes_actually_match_something(ref, server, tool):
    """反向：被接受的形狀必須真的匹配得到目標，否則放行的是死字串。"""
    assert matches_tool_ref(ref, server, tool) is True
