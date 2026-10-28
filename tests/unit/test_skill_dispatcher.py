"""system-skill-dispatcher 動態生成測試 (ADR-0066 Phase 3).

驗證 sync_skill_dispatcher 依當前已載入技能動態生成「可用技能清單」，
寫入 system-skill-dispatcher/SKILL.md，供 Agent 讀清單後 activate_skill。
仿 sync_system_dispatcher 範式。
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service

_DISPATCHER_MD = ("system-skill-dispatcher", "SKILL.md")
_SRC_ROOT = Path(__file__).resolve().parents[2] / "ToukaProxy" / "src"
_SYNC_NAMES = frozenset({"sync_system_dispatcher", "sync_skill_dispatcher"})


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


# --- 產生時機與「沒變不寫」（ADR-0290）---


class _ImportTimeSyncCalls(ast.NodeVisitor):
    """收集 import 時就會執行的兩個 sync 呼叫：模組層與 class 本體算，函式本體不算。"""

    def __init__(self) -> None:
        self.lines: list[int] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_definition_time(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_definition_time(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)

    def _visit_definition_time(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # 裝飾器與預設值在定義時就執行
        for child in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if child is not None:
                self.visit(child)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in _SYNC_NAMES:
            self.lines.append(node.lineno)
        self.generic_visit(node)


def test_no_module_syncs_dispatchers_at_import():
    """載入任何模組都不寫兩個調度員檔：產生只在 lifespan（設定自癒與 MCP 名稱遷移之後）。"""
    paths = sorted(_SRC_ROOT.rglob("*.py"))
    assert paths
    offenders: list[str] = []
    for path in paths:
        visitor = _ImportTimeSyncCalls()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        rel = path.relative_to(_SRC_ROOT).as_posix()
        offenders += [f"{rel}:{line}" for line in visitor.lines]
    assert offenders == []


def _dispatcher_path(skills_dir):
    return skills_dir / _DISPATCHER_MD[0] / _DISPATCHER_MD[1]


def _sync_watching_writes(svc, skills_dir):
    """再 sync 一次，回傳寫到調度員檔的次數。"""
    real_write = resource_manager.write_file_text
    with patch.object(resource_manager, "write_file_text", wraps=real_write) as spy:
        svc.sync_skill_dispatcher()
    target = _dispatcher_path(skills_dir)
    return sum(1 for call in spy.call_args_list if Path(call.args[0]) == target)


@pytest.fixture
def synced(svc, tmp_path):
    """有一個業務技能、已 sync 過一次的 skills 目錄。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "biz", ["name: biz", "description: 業務"], "body")
    svc.load_config()
    svc.sync_skill_dispatcher()
    return skills_dir


def test_dispatcher_resync_unchanged_skips_write(svc, synced):
    """清單沒變再 sync 不重寫（重寫的檔會被 Windows 的 git 誤判成改過）。"""
    assert _sync_watching_writes(svc, synced) == 0


def test_dispatcher_crlf_copy_counts_as_unchanged(svc, synced):
    """舊檔只差換行（Windows 原生寫出 CRLF）也算沒變；逐位元組比較會每次重寫。"""
    path = _dispatcher_path(synced)
    lf = resource_manager.read_file_binary(path).replace(b"\r\n", b"\n")
    resource_manager.write_file_binary(path, lf.replace(b"\n", b"\r\n"))
    assert _sync_watching_writes(svc, synced) == 0


def test_dispatcher_rewrites_stale_content(svc, synced):
    """舊檔內容不同就重寫。"""
    resource_manager.write_file_text(_dispatcher_path(synced), "stale")
    assert _sync_watching_writes(svc, synced) == 1
    assert 'id="biz"' in _read_dispatcher(synced)


def test_dispatcher_rewrites_unreadable_file(svc, synced):
    """舊檔讀不到（不是 UTF-8）時照寫，壞掉的檔才修得回來。"""
    resource_manager.write_file_binary(_dispatcher_path(synced), b"\xff\xfe broken")
    assert _sync_watching_writes(svc, synced) == 1
    assert 'id="biz"' in _read_dispatcher(synced)


def test_dispatcher_skip_still_refreshes_metadata(svc, synced):
    """沒重寫時，記憶體裡的技能清單照樣有調度員（list_skills 靠它）。"""
    svc._metadata.pop("system-skill-dispatcher")
    assert _sync_watching_writes(svc, synced) == 0
    assert "system-skill-dispatcher" in svc._metadata
