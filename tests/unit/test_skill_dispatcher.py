"""system-skill-dispatcher 動態生成測試 (ADR-0066 Phase 3).

驗證 sync_skill_dispatcher 依當前已載入技能動態生成「可用技能清單」，
寫入 system-skill-dispatcher/SKILL.md，供 Agent 讀清單後 activate_skill。
仿 sync_system_dispatcher 範式。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service

_DISPATCHER_MD = ("system-skill-dispatcher", "SKILL.md")


def _write_skill(skills_dir, sid, fm_lines, body):
    skill_dir = skills_dir / sid
    resource_manager.make_dir(skill_dir)
    content = "---\n" + "\n".join(fm_lines) + "\n---\n" + body
    resource_manager.write_file_text(skill_dir / "SKILL.md", content)


def _read_dispatcher(skills_dir):
    return resource_manager.read_file_text(skills_dir / _DISPATCHER_MD[0] / _DISPATCHER_MD[1])


@pytest.fixture
def svc(tmp_path):
    skills_dir = tmp_path / "skills"
    resource_manager.make_dir(skills_dir)
    skill_service._skills_dir = skills_dir
    skill_service.config_path = str(skills_dir)
    skill_service._metadata = {}
    skill_service.load_config()
    return skill_service


def test_dispatcher_lists_non_system_skills(svc, tmp_path):
    """有 2 個非系統 skill → dispatcher body 含兩者的清單項。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "skill-a",
                 ["name: skill-a", "display-name: 技能A", "description: 負責做 A"], "body-a")
    _write_skill(skills_dir, "skill-b",
                 ["name: skill-b", "display-name: 技能B", "description: 負責做 B"], "body-b")
    svc.load_config()
    svc.sync_skill_dispatcher()
    md = _read_dispatcher(skills_dir)
    assert 'id="skill-a"' in md
    assert 'id="skill-b"' in md
    assert "負責做 A" in md
    assert "技能B" in md


def test_dispatcher_excludes_system_skills(svc, tmp_path):
    """清單排除 is_system skill（不列系統技能 / dispatcher 自己）。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "biz", ["name: biz", "description: 業務技能"], "body")
    _write_skill(skills_dir, "sys-x",
                 ["name: sys-x", "is_system: true", "description: 系統技能"], "body")
    svc.load_config()
    svc.sync_skill_dispatcher()
    md = _read_dispatcher(skills_dir)
    assert 'id="biz"' in md
    assert 'id="sys-x"' not in md
    # dispatcher 不列自己
    assert 'id="system-skill-dispatcher"' not in md


def test_dispatcher_body_has_activate_guidance(svc, tmp_path):
    """body 含 activate_skill 使用指引。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "biz", ["name: biz", "description: 業務"], "body")
    svc.load_config()
    svc.sync_skill_dispatcher()
    md = _read_dispatcher(skills_dir)
    assert "activate_skill" in md


def test_dispatcher_version_is_content_hash(svc, tmp_path):
    """version 為內容雜湊（非 epoch）：同技能清單重生→位元組相同（不弄髒 git）；
    清單變→version 與內容變。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "biz", ["name: biz", "description: 業務"], "body")
    svc.load_config()
    svc.sync_skill_dispatcher()
    md1 = _read_dispatcher(skills_dir)
    svc.sync_skill_dispatcher()
    md2 = _read_dispatcher(skills_dir)
    assert md1 == md2  # 同清單重生 → 位元組相同
    assert "version:" in md1

    _write_skill(skills_dir, "extra", ["name: extra", "description: 額外"], "body")
    svc.load_config()
    svc.sync_skill_dispatcher()
    md3 = _read_dispatcher(skills_dir)
    assert md3 != md1  # 清單變 → 內容（含 version 雜湊）變


def test_dispatcher_idempotent_reflects_current_skills(svc, tmp_path):
    """skill 移除後 re-sync → 清單不殘留舊 skill。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "skill-a", ["name: skill-a", "description: A"], "body")
    svc.load_config()
    svc.sync_skill_dispatcher()
    assert 'id="skill-a"' in _read_dispatcher(skills_dir)

    # 移除 skill-a 後 re-sync
    resource_manager.remove_dir(skills_dir / "skill-a")
    svc._metadata.pop("skill-a", None)
    svc.sync_skill_dispatcher()
    assert 'id="skill-a"' not in _read_dispatcher(skills_dir)
