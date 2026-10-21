"""SkillParser 純函式單元測試 (ADR-0066 Phase 1 + Phase 4).

驗證 SKILL.md frontmatter/body 的拆分、解析、序列化與契約映射。
Phase 4：Touka 擴充欄位改入 metadata 子欄位（完全相容 Anthropic quick_validate），
read_touka_field 向後相容舊 top-level 格式。
純函式，零 IO、零 env，免 sandbox。
"""

from __future__ import annotations

from src.services import skill_parser

# --- 測試素材 (Phase 4: 擴充欄位在 metadata 子欄位) ---

STD_SKILL_MD = """---
name: demo-skill
description: 一個示範用的技能
allowed-tools: [Read, Bash]
metadata:
  display-name: 示範技能
  category: Dev
  author: Touka System
  is_system: false
---
你是示範助手。
請協助使用者完成任務。"""

STD_BODY = "你是示範助手。\n請協助使用者完成任務。"

# 舊格式 (top-level 擴充欄位)，用於驗證向後相容 fallback
LEGACY_SKILL_MD = """---
name: legacy-skill
display-name: 舊格式技能
description: top-level 擴充欄位
category: System
author: Touka System
is_system: true
---
舊格式 body。"""


# --- 1. split_frontmatter ---

def test_split_standard_frontmatter():
    """標準 frontmatter → (frontmatter_text, body)。"""
    fm_text, body = skill_parser.split_frontmatter(STD_SKILL_MD)
    assert "name: demo-skill" in fm_text
    assert body == STD_BODY


def test_split_no_frontmatter_returns_empty_and_raw():
    """無 frontmatter → ("", raw)。"""
    raw = "# 純 Markdown\n沒有 frontmatter。"
    fm_text, body = skill_parser.split_frontmatter(raw)
    assert fm_text == ""
    assert body == raw


def test_split_body_with_internal_dashes_not_miscut():
    """body 內含 --- (表格分隔線/水平線) 不應誤切。"""
    raw = (
        "---\n"
        "name: tbl\n"
        "---\n"
        "# 標題\n"
        "| a | b |\n"
        "|---|---|\n"
        "---\n"
        "結尾段落"
    )
    fm_text, body = skill_parser.split_frontmatter(raw)
    assert fm_text == "name: tbl"
    assert "|---|---|" in body
    assert "結尾段落" in body
    assert body.startswith("# 標題")


# --- 2. parse_frontmatter ---

def test_parse_allowed_tools_as_list():
    """allowed-tools: [Read, Bash] → list[str]。"""
    fm = skill_parser.parse_frontmatter("allowed-tools: [Read, Bash]")
    assert fm["allowed-tools"] == ["Read", "Bash"]


def test_parse_nested_metadata():
    """metadata 子欄位被解析為巢狀 dict。"""
    fm = skill_parser.parse_frontmatter(
        "name: x\nmetadata:\n  is_system: true\n  author: T"
    )
    assert fm["metadata"]["is_system"] is True
    assert fm["metadata"]["author"] == "T"


# --- 3. read_touka_field (Phase 4) ---

def test_read_touka_field_prefers_metadata():
    """read_touka_field 優先讀 metadata 子欄位。"""
    fm = {"name": "x", "metadata": {"display-name": "從metadata"}}
    assert skill_parser.read_touka_field(fm, "display-name") == "從metadata"


def test_read_touka_field_fallback_top_level():
    """無 metadata 時 fallback 讀 top-level (向後相容舊檔)。"""
    fm = {"name": "x", "display-name": "從top"}
    assert skill_parser.read_touka_field(fm, "display-name") == "從top"


def test_read_touka_field_default():
    """metadata 與 top-level 皆無 → 回 default。"""
    fm = {"name": "x"}
    assert skill_parser.read_touka_field(fm, "author", "預設") == "預設"


# --- 4. build_frontmatter (Phase 4) ---

def test_build_frontmatter_structure():
    """name/description/allowed-tools top-level；其餘入 metadata。"""
    fm = skill_parser.build_frontmatter(
        name="demo", description="d", display_name="顯示",
        category="Dev", author="T", is_system=False,
        allowed_tools=["Read"], version=123.0,
    )
    assert fm["name"] == "demo"
    assert fm["description"] == "d"
    assert fm["allowed-tools"] == ["Read"]
    # 擴充欄位都在 metadata
    assert fm["metadata"]["display-name"] == "顯示"
    assert fm["metadata"]["category"] == "Dev"
    assert fm["metadata"]["is_system"] is False
    assert fm["metadata"]["version"] == 123.0
    # 擴充欄位不在 top-level
    assert "display-name" not in fm
    assert "is_system" not in fm


def test_build_frontmatter_omits_empty_allowed_tools():
    """無 allowed_tools 時不寫該 key。"""
    fm = skill_parser.build_frontmatter(
        name="demo", description="d", display_name="顯示",
        category="Dev", author="T", is_system=False,
    )
    assert "allowed-tools" not in fm


# --- 5. map_to_contract ---

def test_map_contains_all_nine_keys():
    """map_to_contract 結果含全部 9 個契約 key。"""
    fm, body = skill_parser.parse_skill_md(STD_SKILL_MD)
    contract = skill_parser.map_to_contract(fm, body, ["touka-dispatch"])
    for key in (
        "id", "name", "description", "system_prompt",
        "required_tools", "mcp_servers", "is_system", "author", "category",
    ):
        assert key in contract, f"缺少契約 key: {key}"


def test_map_allowed_tools_to_required_tools():
    """allowed-tools → required_tools；缺則 []。"""
    fm, body = skill_parser.parse_skill_md(STD_SKILL_MD)
    contract = skill_parser.map_to_contract(fm, body, [])
    assert contract["required_tools"] == ["Read", "Bash"]

    fm2 = {"name": "no-tools"}
    contract2 = skill_parser.map_to_contract(fm2, "body", [])
    assert contract2["required_tools"] == []


def test_map_system_prompt_equals_body():
    """system_prompt == body。"""
    fm, body = skill_parser.parse_skill_md(STD_SKILL_MD)
    contract = skill_parser.map_to_contract(fm, body, [])
    assert contract["system_prompt"] == STD_BODY


def test_map_name_from_metadata_display_name():
    """name = metadata.display-name (kebab id 作 fallback)。"""
    fm, body = skill_parser.parse_skill_md(STD_SKILL_MD)
    contract = skill_parser.map_to_contract(fm, body, [])
    assert contract["name"] == "示範技能"
    assert contract["id"] == "demo-skill"


def test_map_legacy_top_level_fields_fallback():
    """舊格式 top-level 擴充欄位仍正確映射 (向後相容)。"""
    fm, body = skill_parser.parse_skill_md(LEGACY_SKILL_MD)
    contract = skill_parser.map_to_contract(fm, body, [])
    assert contract["id"] == "legacy-skill"
    assert contract["name"] == "舊格式技能"
    assert contract["is_system"] is True
    assert contract["author"] == "Touka System"
    assert contract["category"] == "System"


def test_map_missing_fields_safe_defaults():
    """缺 author/category/is_system → 安全預設。"""
    fm = {"name": "minimal"}
    contract = skill_parser.map_to_contract(fm, "body", [])
    assert contract["author"] == ""
    assert contract["category"] == "General"
    assert contract["is_system"] is False
    assert contract["description"] == ""


# --- 6. serialize round-trip ---

def test_serialize_round_trip_with_build_frontmatter():
    """build_frontmatter → serialize → parse 還原相同 frontmatter 與 body。"""
    fm = skill_parser.build_frontmatter(
        name="round-trip", description="驗證可逆", display_name="往返測試",
        category="System", author="T", is_system=True, allowed_tools=["Read", "Grep"],
    )
    body = "第一行\n第二行\n含中文與符號 :- |"
    serialized = skill_parser.serialize_skill_md(fm, body)
    parsed_fm, parsed_body = skill_parser.parse_skill_md(serialized)
    assert parsed_fm == fm
    assert parsed_body == body
    # 映射後擴充欄位正確
    contract = skill_parser.map_to_contract(parsed_fm, parsed_body, [])
    assert contract["name"] == "往返測試"
    assert contract["is_system"] is True
