"""SkillService Progressive Disclosure 測試 (ADR-0066 Phase 2).

驗證三層載入：
  第 1 層 list_skills 不含 system_prompt body
  第 2 層 get_skill 含 system_prompt body
  第 3 層 get_skill_resource / list_skill_resources 按需讀取 (含目錄逃逸防護)
"""

from __future__ import annotations

import pytest
from src.services.skill_service import skill_service


def _write_skill(skills_dir, sid, fm_lines, body):
    skill_dir = skills_dir / sid
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = "---\n" + "\n".join(fm_lines) + "\n---\n" + body
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


@pytest.fixture
def svc(tmp_path):
    """指向 tmp_path 的 SkillService 單例。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_service._skills_dir = skills_dir
    skill_service.config_path = str(skills_dir)
    skill_service._metadata = {}
    skill_service.load_config()
    return skill_service


# --- 第 1 / 2 層分離 ---

def test_list_skills_omits_system_prompt(svc, tmp_path):
    """第 1 層：list_skills 結果不含 system_prompt。"""
    _write_skill(tmp_path / "skills", "demo", ["name: demo"], "敏感的完整指令內容")
    svc.load_config()
    entry = svc.list_skills()[0]
    assert "system_prompt" not in entry


def test_get_skill_includes_system_prompt(svc, tmp_path):
    """第 2 層：get_skill 含 system_prompt。"""
    _write_skill(tmp_path / "skills", "demo", ["name: demo"], "完整指令內容")
    svc.load_config()
    assert svc.get_skill("demo")["system_prompt"] == "完整指令內容"


# --- 第 3 層 Resources ---

def test_get_skill_resource_reads_reference(svc, tmp_path):
    """get_skill_resource 讀取 references/ 下檔案內容。"""
    skill_dir = _write_skill(tmp_path / "skills", "demo", ["name: demo"], "body")
    ref_dir = skill_dir / "references"
    ref_dir.mkdir()
    (ref_dir / "foo.md").write_text("參考資料內容", encoding="utf-8")
    svc.load_config()
    assert svc.get_skill_resource("demo", "references/foo.md") == "參考資料內容"


def test_get_skill_resource_blocks_path_escape(svc, tmp_path):
    """get_skill_resource 對 ../ 目錄逃逸回 None。"""
    _write_skill(tmp_path / "skills", "demo", ["name: demo"], "body")
    # 在 skills 目錄外放一個機密檔
    (tmp_path / "secret.txt").write_text("機密", encoding="utf-8")
    svc.load_config()
    assert svc.get_skill_resource("demo", "../../secret.txt") is None


def test_get_skill_resource_nonexistent_returns_none(svc, tmp_path):
    """不存在的資源 → None。"""
    _write_skill(tmp_path / "skills", "demo", ["name: demo"], "body")
    svc.load_config()
    assert svc.get_skill_resource("demo", "references/ghost.md") is None


def test_list_skill_resources_enumerates(svc, tmp_path):
    """list_skill_resources 列舉 scripts/references/assets。"""
    skill_dir = _write_skill(tmp_path / "skills", "demo", ["name: demo"], "body")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "doc.md").write_text("doc", encoding="utf-8")
    svc.load_config()
    resources = svc.list_skill_resources("demo")
    assert "scripts/run.py" in resources
    assert "references/doc.md" in resources
