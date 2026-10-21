"""document-generator 薄技能種子驗證（frontmatter + .mcp.json 對齊 mcp-docgen）。

技能正文教 agent 使用 mcp-docgen 的三個工具；`.mcp.json` 的 top-level key 必須
等於 Hub install 產生的 server key（mcp-docgen），否則 ensure_connected 接不到。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.services import skill_parser

_SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "ToukaProxy"
    / "config"
    / "skills"
    / "document-generator"
)


def test_skill_frontmatter_is_valid():
    fm, body = skill_parser.parse_skill_md(
        (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    )
    assert fm["name"] == "document-generator"
    assert skill_parser.read_touka_field(fm, "is_system") is False
    assert skill_parser.read_touka_field(fm, "display-name")
    assert body.strip()


def test_mcp_json_declares_mcp_docgen():
    data = json.loads((_SKILL_DIR / ".mcp.json").read_text(encoding="utf-8"))
    assert list(data.keys()) == ["mcp-docgen"]
    assert data["mcp-docgen"]["command"] == "uvx"
    assert data["mcp-docgen"]["args"] == ["mcp-docgen"]
