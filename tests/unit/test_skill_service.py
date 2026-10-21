"""SkillService 既有 schema 測試 (適配 ADR-0066 目錄格式)。

get_all_skills_schema 改由第 1 層 _metadata (frontmatter) 產生；
本測試注入 metadata 驗證 schema 輸出結構不變。
"""

from __future__ import annotations

import pytest
from src.services.skill_service import skill_service


@pytest.fixture
def svc():
    """注入 metadata 的 SkillService 單例 (get_all_skills_schema 只讀 _metadata)。"""
    skill_service._metadata = {
        "web_search": {
            "name": "web_search", "display-name": "Web Search",
            "capability": "web_access",
        },
        "code_interpreter": {
            "name": "code_interpreter", "display-name": "Code Interpreter",
            "capability": "code_execution",
        },
        "system_tool": {
            "name": "system_tool", "display-name": "System Tool",
            "is_system": True,
        },
    }
    return skill_service


def test_get_all_skills_schema(svc):
    """驗證技能 Schema 的獲取。"""
    schema = svc.get_all_skills_schema()
    assert len(schema) == 3
    assert any(s["id"] == "web_search" for s in schema)
