"""ADR-0148：skill_service.render_skill_registry 的 RBAC 過濾契約（廣告面＝授權面）。

render_skill_registry 只列給定 skill_ids（SkillLayer 依 can_use_skill 算出），
杜絕「看得到用不了」。系統技能／未知 id 不列；無可列項回空字串（→ 不注入 dispatcher）。
"""

from __future__ import annotations

import pytest
from src.services import skill_parser
from src.services.skill_service import skill_service


def _fm(name: str, *, is_system: bool = False, desc: str = "d") -> dict:
    return skill_parser.build_frontmatter(
        name=name, description=desc, display_name=name, category="Test",
        author="t", is_system=is_system,
    )


@pytest.fixture(autouse=True)
def _seed_metadata():
    """注入測試技能 metadata；測後還原（skill_service 為單例）。"""
    original = dict(skill_service._metadata)  # noqa: SLF001
    skill_service._metadata.update(  # noqa: SLF001
        {
            "alpha": _fm("alpha"),
            "beta": _fm("beta"),
            "sys-skill": _fm("sys-skill", is_system=True),
        }
    )
    yield
    skill_service._metadata.clear()  # noqa: SLF001
    skill_service._metadata.update(original)  # noqa: SLF001


def test_render_lists_only_given_ids():
    out = skill_service.render_skill_registry(["alpha"])
    assert 'id="alpha"' in out
    assert 'id="beta"' not in out
    assert "<skill_registry>" in out
    assert "activate_skill" in out  # 調度員本體提示保留


def test_render_empty_string_for_no_ids():
    assert skill_service.render_skill_registry([]) == ""


def test_render_skips_system_and_unknown():
    # 系統技能與不存在的 id 都不列；本例全被濾掉 → 空字串
    assert skill_service.render_skill_registry(["sys-skill", "nonexistent"]) == ""


def test_render_multiple_usable():
    out = skill_service.render_skill_registry(["alpha", "beta"])
    assert 'id="alpha"' in out
    assert 'id="beta"' in out


def test_dispatcher_body_wrapped_in_capabilities():
    """dispatcher_body 併入同一 <capabilities><skill name="system-skill-dispatcher">
    （ADR-0148 對抗式審查 Finding 3：維持 context_breakdown 的 tag-SSOT 歸因）。"""

    class _Req:
        system = ""

    req = _Req()
    skill_service.apply_skills_to_request(
        req, [], dispatcher_body="<skill_registry>X</skill_registry>"
    )
    assert "<capabilities>" in req.system
    assert 'name="system-skill-dispatcher"' in req.system
    assert "<skill_registry>X</skill_registry>" in req.system

