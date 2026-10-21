"""classify_mcp_category 與 MCP_HUB_CATEGORIES（功能性分類 SSOT）單元測試。

分類頁籤修復：後端把 hub server 分到前端 taxonomy（Browser/Dev/File/Database/Utils），
取代原本只給 Official/Community、造成前端頁籤永遠空白的 bug。classifier 為純函式，
核心不變式：**永遠回傳 MCP_HUB_CATEGORIES 的成員**（未知一律落 Utils catch-all）。
"""
from __future__ import annotations

import pytest
from src.utils.mcp_category import MCP_HUB_CATEGORIES, classify_mcp_category


def test_categories_are_the_frontend_taxonomy():
    """SSOT 常數須與前端頁籤 taxonomy 完全一致。"""
    assert MCP_HUB_CATEGORIES == ("Browser", "Dev", "File", "Database", "Utils")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("filesystem", "File"),
        ("Google Drive", "File"),
        ("Google Sheets", "File"),
        ("git", "Dev"),
        ("github", "Dev"),
        ("fetch", "Browser"),
        ("Brave Search", "Browser"),
        ("Exa Search", "Browser"),
        ("supabase", "Database"),
        ("postgres", "Database"),
        ("memory", "Utils"),
        ("time", "Utils"),
        ("sequentialthinking", "Utils"),
        ("everything", "Utils"),
    ],
)
def test_representative_mappings(name, expected):
    """代表性 server 名稱應落到直覺分類。"""
    assert classify_mcp_category(name) == expected


@pytest.mark.parametrize(
    "name",
    ["", "totally-unknown-thing", "weather", "horoscope", "random widget"],
)
def test_unknown_defaults_to_utils(name):
    """無關鍵字命中 → catch-all Utils。"""
    assert classify_mcp_category(name) == "Utils"


@pytest.mark.parametrize(
    ("name", "description", "npm"),
    [
        ("Whatever", "A postgres database wrapper", "pg-mcp"),
        ("X", "", "playwright-mcp"),
        ("", "search the web", ""),
    ],
)
def test_uses_description_and_npm(name, description, npm):
    """分類同時參考 name/description/npm_package，結果恆為合法分類。"""
    assert classify_mcp_category(name, description, npm) in MCP_HUB_CATEGORIES


def test_always_returns_valid_category():
    """不變式：任意輸入都只回傳 taxonomy 內的值（杜絕孤兒分類重演）。"""
    samples = [
        "a", "ZZZ", "drive files", "redis cache", "fetch url",
        "git repo", "memory store", "1234", "！＠＃", "PDF tool",
    ]
    for s in samples:
        assert classify_mcp_category(s) in MCP_HUB_CATEGORIES


def test_substring_false_positive_guarded():
    """word-boundary：'digital'/'legitimate' 不應誤命中 'git' → Dev。"""
    assert classify_mcp_category("digital marketing assistant") == "Utils"
