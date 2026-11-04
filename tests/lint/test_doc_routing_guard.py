"""規則分流的路徑腳本與 Guard #46（ADR-0296）。

合成 repo 都建在 tmp_path；真實 repo 只唯讀（`git ls-files`、讀追蹤中的文件）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from src.services import execution_broker

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
GIT = shutil.which("git")


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rules = _load("applicable_rules")
guard = _load("guard_doc_routing")

AGENTS = """\
## 開頭

## 🚧 開發禁令清單

*   **禁令 1**〔模組：alpha、beta〕：綁路徑的摘要。全文：docs/rules/alpha.md
*   **禁令 2（全系統的）**〔全系統〕：全文就在這裡。
*   **禁令 3（已移除）— 🗑️ 已退役**〔退役〕：墓碑。
*   **禁令 4**〔模組：beta〕：全文還留在這一行。

## 核心

### 1. 搬走的小節
〔模組：beta、alpha〕小節摘要。全文：docs/rules/beta.md

### 2. 留著的小節
*   留著的內容。

### 3. 拆開的小節
*   留著的子項。
〔模組：alpha〕其餘子項。全文：docs/rules/alpha.md
"""
ALPHA = """\
# Alpha 規則

*   **禁令 1（完整名稱）**：綁路徑的全文。

### 3. 拆開的小節
*   搬走的子項。
"""
BETA = """\
# Beta 規則

### 1. 搬走的小節
*   小節全文。
"""
CONFIG: dict[str, Any] = {
    "modules": {
        "alpha": {
            "globs": ["alpha/**"],
            "rules_file": "docs/rules/alpha.md",
            "handover": "docs/handover/alpha.md",
        },
        "beta": {
            "globs": ["beta/**", "**/*.cfg"],
            "rules_file": "docs/rules/beta.md",
            "handover": "docs/handover/beta.md",
        },
    },
    "not_applicable": ["docs/**", "AGENTS.md", "HANDOVER.md"],
    "bans": {"1": ["alpha/special.py", "beta/app.cfg"], "4": ["beta/**"]},
    "sections": {"1. 搬走的小節": ["beta/**", "alpha/section.py"], "3. 拆開的小節": ["alpha/**"]},
    "inline_rules": ["禁令 4"],
}
TRACKED = [
    "AGENTS.md",
    "HANDOVER.md",
    "alpha/plain.py",
    "alpha/section.py",
    "alpha/special.py",
    "beta/one.py",
    "beta/app.cfg",
    "docs/handover/alpha.md",
    "docs/handover/beta.md",
    "docs/rules/alpha.md",
    "docs/rules/beta.md",
    "docs/rules/modules.json",
]


def _write_repo(root: Path, *, agents: str = AGENTS, alpha: str = ALPHA, beta: str = BETA) -> Path:
    (root / "docs" / "rules").mkdir(parents=True)
    (root / "docs" / "handover").mkdir(parents=True)
    (root / "AGENTS.md").write_text(agents, encoding="utf-8")
    (root / "HANDOVER.md").write_text("# 交班\n", encoding="utf-8")
    (root / "docs" / "rules" / "alpha.md").write_text(alpha, encoding="utf-8")
    (root / "docs" / "rules" / "beta.md").write_text(beta, encoding="utf-8")
    (root / "docs" / "rules" / "modules.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    (root / "docs" / "handover" / "alpha.md").write_text("# a\n", encoding="utf-8")
    (root / "docs" / "handover" / "beta.md").write_text("# b\n", encoding="utf-8")
    return root


def _errors(root: Path, tracked: list[str] | None = None) -> list[str]:
    return guard.check(root, TRACKED if tracked is None else tracked).errors


def _git(cwd: Path, *args: str) -> str:
    assert GIT is not None
    result = execution_broker.run_sync(
        [GIT, "-C", str(cwd), "-c", "core.quotepath=true", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """有 origin/dev 的合成 repo：`keep.py`、`gone.py` 已提交。"""
    if not GIT:
        pytest.skip("git is unavailable")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=dev")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "keep.py").write_text("a\n", encoding="utf-8")
    (repo / "gone.py").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "keep.py", "gone.py")
    _git(repo, "commit", "--quiet", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    return repo


# ── 比對語意 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("ToukaChat/**", "ToukaChat/src/a/b.ts", True),
        ("ToukaChat/**", "ToukaChatOther/a.ts", False),
        ("ToukaChat/**", "toukachat/a.ts", False),
        ("**/*.py", "root.py", True),
        ("**/*.py", "a/b/c.py", True),
        ("**/*.py", "a/b/c.pyc", False),
        ("docker-compose*.yml", "docker-compose.local.yml", True),
        ("docker-compose*.yml", "sub/docker-compose.yml", False),
        ("**/Dockerfile*", "ToukaProxy/Dockerfile", True),
        ("src/hooks/use*", "src/hooks/useChat.ts", True),
        ("src/hooks/use*", "src/hooks/deep/useChat.ts", False),
        ("a.b", "aXb", False),
    ],
)
def test_glob_semantics_are_slash_separated_and_case_sensitive(
    pattern: str, path: str, expected: bool
) -> None:
    assert rules.matches(pattern, path) is expected


# ── 分流結果 ────────────────────────────────────────────────


def _keys(route: Any) -> list[str]:
    return [key for key, _ in route.items]


def _route(root: Path, *paths: str) -> Any:
    return rules.route(paths, rules.load_config(root), rules.parse_tags(root))


def test_route_is_precise_to_each_rule_not_the_whole_module(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)

    plain = _route(root, "alpha/plain.py")
    assert plain.modules == ["alpha"]
    assert _keys(plain) == ["3. 拆開的小節"]

    special = _route(root, "alpha/special.py")
    assert _keys(special) == ["禁令 1", "3. 拆開的小節"]
    assert special.handovers == ["docs/handover/alpha.md"]


def test_route_reports_where_each_full_text_lives(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    route = _route(root, "beta/one.py", "alpha/special.py")
    assert route.modules == ["alpha", "beta"]
    assert dict(route.items) == {
        "禁令 1": "docs/rules/alpha.md",
        "禁令 4": "AGENTS.md",
        "1. 搬走的小節": "docs/rules/beta.md",
        "3. 拆開的小節": "docs/rules/alpha.md",
    }


def test_docs_only_change_lists_only_system_rules(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    route = _route(root, "docs/x.md", "HANDOVER.md")
    assert route.items == []
    assert route.modules == []
    assert route.unknown == []
    assert "全系統" in rules.render(route)


@pytest.mark.parametrize(
    "path", ["brand-new/file.py", "../outside.py", "/abs/file.py"]
)
def test_unknown_path_asks_to_read_every_rules_file(tmp_path: Path, path: str) -> None:
    root = _write_repo(tmp_path)
    route = _route(root, path)
    assert route.unknown == [path.replace("\\", "/")]
    text = rules.render(route)
    assert "讀全部規則檔" in text
    assert "docs/rules/alpha.md" in text
    assert "docs/rules/beta.md" in text
    assert "docs/handover/alpha.md" in text


def test_windows_separators_and_leading_dot_are_normalized(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    assert _keys(_route(root, "alpha\\special.py")) == ["禁令 1", "3. 拆開的小節"]
    assert _keys(_route(root, "./alpha/special.py")) == ["禁令 1", "3. 拆開的小節"]


def test_print_rules_returns_only_the_listed_full_text(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    text = rules.full_text(_route(root, "beta/one.py"), root)
    assert "全文還留在這一行" in text
    assert "小節全文" in text
    assert "綁路徑的全文" not in text
    assert "搬走的子項" not in text


# ── 取得改動清單 ────────────────────────────────────────────


def test_changed_paths_cover_unstaged_staged_and_untracked(git_repo: Path) -> None:
    (git_repo / "keep.py").write_text("changed\n", encoding="utf-8")
    (git_repo / "staged.py").write_text("x\n", encoding="utf-8")
    _git(git_repo, "add", "staged.py")
    (git_repo / "untracked.py").write_text("y\n", encoding="utf-8")
    assert rules.changed_paths(git_repo) == ["keep.py", "staged.py", "untracked.py"]


def test_changed_paths_include_committed_and_deleted(git_repo: Path) -> None:
    (git_repo / "new.py").write_text("x\n", encoding="utf-8")
    _git(git_repo, "rm", "--quiet", "gone.py")
    _git(git_repo, "add", "new.py")
    _git(git_repo, "commit", "--quiet", "-m", "work")
    assert rules.changed_paths(git_repo) == ["gone.py", "new.py"]


def test_staged_change_reverted_in_worktree_still_counts(git_repo: Path) -> None:
    (git_repo / "keep.py").write_text("changed\n", encoding="utf-8")
    _git(git_repo, "add", "keep.py")
    (git_repo / "keep.py").write_text("a\n", encoding="utf-8")
    assert rules.changed_paths(git_repo) == ["keep.py"]


def test_rename_lists_both_old_and_new_path(git_repo: Path) -> None:
    _git(git_repo, "mv", "keep.py", "renamed.py")
    assert rules.changed_paths(git_repo) == ["keep.py", "renamed.py"]


def test_non_ascii_path_is_not_escaped(git_repo: Path) -> None:
    (git_repo / "中文檔.py").write_text("x\n", encoding="utf-8")
    assert rules.changed_paths(git_repo) == ["中文檔.py"]


def test_no_base_fails_instead_of_reporting_no_changes(git_repo: Path) -> None:
    _git(git_repo, "update-ref", "-d", "refs/remotes/origin/dev")
    with pytest.raises(rules.RoutingError):
        rules.changed_paths(git_repo)


def test_main_exits_nonzero_when_base_is_unknown(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_repo(git_repo)
    _git(git_repo, "update-ref", "-d", "refs/remotes/origin/dev")
    assert rules.main([], root=git_repo) == 2
    assert "基準" in capsys.readouterr().err


def test_explicit_base_is_honored(git_repo: Path) -> None:
    _git(git_repo, "update-ref", "-d", "refs/remotes/origin/dev")
    first = _git(git_repo, "rev-parse", "HEAD").strip()
    (git_repo / "later.py").write_text("x\n", encoding="utf-8")
    assert rules.changed_paths(git_repo, base=first) == ["later.py"]


# ── 守衛：標籤與全文 ────────────────────────────────────────


def test_guard_accepts_a_consistent_repo(tmp_path: Path) -> None:
    assert _errors(_write_repo(tmp_path)) == []


def test_guard_fails_on_missing_tag(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, agents=AGENTS.replace("〔全系統〕", ""))
    assert any("禁令 2" in e and "標籤" in e for e in _errors(root))


def test_guard_fails_on_skipped_number(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, agents=AGENTS.replace("禁令 3（已移除）", "禁令 5（已移除）"))
    assert any("禁令 3" in e for e in _errors(root))


def test_guard_fails_on_unknown_module_tag(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, agents=AGENTS.replace("〔模組：beta〕", "〔模組：gamma〕"))
    assert any("gamma" in e for e in _errors(root))


def test_guard_fails_when_full_text_is_not_in_first_module_file(tmp_path: Path) -> None:
    root = _write_repo(
        tmp_path,
        agents=AGENTS.replace(
            "綁路徑的摘要。全文：docs/rules/alpha.md", "摘要。全文：docs/rules/beta.md"
        ),
    )
    assert any("禁令 1" in e and "第一個模組" in e for e in _errors(root))


def test_guard_fails_when_full_text_is_missing(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, alpha="# Alpha\n\n### 3. 拆開的小節\n*   x\n")
    assert any("禁令 1" in e and "沒有全文" in e for e in _errors(root))


def test_guard_fails_when_full_text_is_duplicated(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, beta=BETA + "\n*   **禁令 1（完整名稱）**：又一份。\n")
    assert any("禁令 1" in e and "重複" in e for e in _errors(root))


def test_guard_fails_when_a_resident_rule_also_has_a_copy(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, alpha=ALPHA + "\n*   **禁令 2（全系統的）**：又一份。\n")
    assert any("禁令 2" in e and "AGENTS.md" in e for e in _errors(root))


def test_guard_fails_when_moved_section_full_text_is_missing(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, beta="# Beta\n")
    assert any("1. 搬走的小節" in e and "沒有全文" in e for e in _errors(root))


def test_guard_fails_when_tagged_rule_has_no_globs(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    config = json.loads(json.dumps(CONFIG))
    del config["bans"]["4"]
    (root / "docs" / "rules" / "modules.json").write_text(json.dumps(config), encoding="utf-8")
    assert any("禁令 4" in e and "沒有它的路徑" in e for e in _errors(root))


def test_guard_fails_when_globs_exist_for_an_untagged_rule(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    config = json.loads(json.dumps(CONFIG))
    config["bans"]["2"] = ["beta/**"]
    (root / "docs" / "rules" / "modules.json").write_text(json.dumps(config), encoding="utf-8")
    assert any("禁令 2" in e and "modules.json" in e for e in _errors(root))


# ── 守衛：路徑涵蓋 ──────────────────────────────────────────


def test_guard_fails_on_tracked_file_that_no_module_recognizes(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    assert any("mystery/x.py" in e for e in _errors(root, [*TRACKED, "mystery/x.py"]))


def test_guard_fails_on_glob_matching_nothing(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    tracked = [p for p in TRACKED if p != "alpha/special.py"]
    assert any("alpha/special.py" in e and "對不到" in e for e in _errors(root, tracked))


def test_guard_fails_when_rules_or_handover_file_is_missing(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    (root / "docs" / "handover" / "beta.md").unlink()
    assert any("docs/handover/beta.md" in e for e in _errors(root))


# ── 守衛：大小 ──────────────────────────────────────────────


def test_over_soft_limit_only_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _write_repo(tmp_path, agents=AGENTS + "x" * guard.AGENTS_SOFT_LIMIT_BYTES)
    report = guard.check(root, TRACKED)
    assert report.errors == []
    assert any("AGENTS.md" in w for w in report.warnings)
    assert guard.main(root=root, tracked=TRACKED) == 0
    assert "WARN" in capsys.readouterr().out


def test_handover_over_soft_limit_only_warns(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    (root / "HANDOVER.md").write_text("x" * (guard.HANDOVER_SOFT_LIMIT_BYTES + 1), encoding="utf-8")
    report = guard.check(root, TRACKED)
    assert report.errors == []
    assert any("HANDOVER.md" in w for w in report.warnings)


def test_last_ban_past_the_read_limit_fails(tmp_path: Path) -> None:
    padding = "x" * guard.CODEX_READ_LIMIT_BYTES + "\n"
    root = _write_repo(tmp_path, agents=padding + AGENTS)
    assert any("32768" in e or "32,768" in e for e in _errors(root))
    assert guard.main(root=root, tracked=TRACKED) == 1


def _agents_with_last_ban_ending_at(limit: int) -> str:
    head, marker, tail = AGENTS.partition("*   **禁令 4**〔模組：beta〕：全文還留在這一行。")
    used = len((head + marker).encode("utf-8"))
    return "x" * (limit - used - 1) + "\n" + head + marker + tail


def test_last_ban_exactly_at_the_limit_passes_and_one_byte_more_fails(tmp_path: Path) -> None:
    limit = guard.CODEX_READ_LIMIT_BYTES
    root = _write_repo(tmp_path, agents=_agents_with_last_ban_ending_at(limit))
    assert _errors(root) == []
    (root / "AGENTS.md").write_text(_agents_with_last_ban_ending_at(limit + 1), encoding="utf-8")
    assert any("超過 Codex" in e for e in _errors(root))


def test_size_is_measured_with_lf_so_ci_and_windows_agree(tmp_path: Path) -> None:
    text = _agents_with_last_ban_ending_at(guard.CODEX_READ_LIMIT_BYTES)
    root = _write_repo(tmp_path)
    (root / "AGENTS.md").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert _errors(root) == []


# ── 守衛：路徑與全文的反向核對 ──────────────────────────────


def test_guard_fails_on_empty_globs(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    config = json.loads(json.dumps(CONFIG))
    config["bans"]["4"] = []
    (root / "docs" / "rules" / "modules.json").write_text(json.dumps(config), encoding="utf-8")
    assert any("禁令 4" in e and "空的也不行" in e for e in _errors(root))


def test_guard_fails_when_globs_miss_a_tagged_module(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    config = json.loads(json.dumps(CONFIG))
    config["bans"]["1"] = ["alpha/special.py"]
    (root / "docs" / "rules" / "modules.json").write_text(json.dumps(config), encoding="utf-8")
    assert any("禁令 1" in e and "模組 beta" in e for e in _errors(root))


def test_guard_fails_when_a_module_rule_is_reduced_to_a_summary(tmp_path: Path) -> None:
    agents = AGENTS.replace("綁路徑的摘要。全文：docs/rules/alpha.md", "只剩一句摘要。")
    alpha = ALPHA.replace("*   **禁令 1（完整名稱）**：綁路徑的全文。\n", "")
    root = _write_repo(tmp_path, agents=agents, alpha=alpha)
    assert any("禁令 1" in e and "只剩摘要" in e for e in _errors(root))


def test_guard_fails_when_paths_leave_the_tagged_modules(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    config = json.loads(json.dumps(CONFIG))
    config["bans"]["4"] = ["beta/**", "alpha/plain.py"]
    (root / "docs" / "rules" / "modules.json").write_text(json.dumps(config), encoding="utf-8")
    assert any("禁令 4" in e and "超出標到的模組" in e for e in _errors(root))


@pytest.mark.parametrize("stale", ["禁令 1", "禁令 2", "不存在的規則"])
def test_guard_fails_on_stale_inline_rule(tmp_path: Path, stale: str) -> None:
    root = _write_repo(tmp_path)
    config = json.loads(json.dumps(CONFIG))
    config["inline_rules"] = ["禁令 4", stale]
    (root / "docs" / "rules" / "modules.json").write_text(json.dumps(config), encoding="utf-8")
    assert any(stale in e and "inline_rules" in e for e in _errors(root))


def test_guard_error_output_survives_a_cp950_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = AGENTS.replace("### 1. 搬走的小節", "### 1. 搬走的小節 ⭐")
    root = _write_repo(tmp_path, agents=agents)
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp950")
    monkeypatch.setattr(sys, "stdout", stream)
    assert guard.main(root, TRACKED) == 1
    stream.flush()
    assert "⭐".encode() in stream.buffer.getvalue()


def test_inline_rules_may_keep_full_text_in_agents(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    assert not any("禁令 4" in e for e in _errors(root))


def test_guard_fails_on_orphan_full_text(tmp_path: Path) -> None:
    alpha = ALPHA + "\n*   **禁令 9（沒人指）**：孤兒。\n\n### 8. 沒人指的小節\n*   孤兒。\n"
    root = _write_repo(tmp_path, alpha=alpha)
    errors = _errors(root)
    assert any("禁令 9" in e and "指回來" in e for e in errors)
    assert any("8. 沒人指的小節" in e and "指回來" in e for e in errors)


def test_guard_fails_when_section_tag_is_removed_but_full_text_stays(tmp_path: Path) -> None:
    agents = AGENTS.replace("〔模組：beta、alpha〕小節摘要。全文：docs/rules/beta.md\n", "")
    root = _write_repo(tmp_path, agents=agents)
    errors = _errors(root)
    assert any("1. 搬走的小節" in e and "指回來" in e for e in errors)
    assert any("1. 搬走的小節" in e and "sections" in e for e in errors)


# ── 命令列 ────────────────────────────────────────────────


def test_main_prints_rules_for_explicit_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _write_repo(tmp_path)
    assert rules.main(["--paths", "alpha/special.py", "--print-rules"], root=root) == 0
    output = capsys.readouterr().out
    assert "禁令 1：docs/rules/alpha.md" in output
    assert "綁路徑的全文" in output


def test_main_expands_a_directory_to_its_tracked_files(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _write_repo(git_repo)
    (root / "alpha").mkdir()
    (root / "alpha" / "special.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "alpha/special.py")
    assert rules.main(["--paths", "alpha"], root=root) == 0
    assert "禁令 1" in capsys.readouterr().out


def test_expand_dirs_lists_tracked_files_and_keeps_new_paths(git_repo: Path) -> None:
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "inner.py").write_text("x = 1\n", encoding="utf-8")
    _git(git_repo, "add", "pkg/inner.py")
    assert rules.expand_dirs(git_repo, ["pkg", "pkg/", "new/none.py"]) == [
        "pkg/inner.py",
        "pkg/inner.py",
        "new/none.py",
    ]


def test_directory_without_tracked_files_stays_as_given(git_repo: Path) -> None:
    (git_repo / "empty").mkdir()
    assert rules.expand_dirs(git_repo, ["empty"]) == ["empty"]


def test_main_hints_when_nothing_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write_repo(tmp_path)
    monkeypatch.setattr(rules, "changed_paths", lambda _root, _base: [])
    assert rules.main([], root=root) == 0
    assert "--paths" in capsys.readouterr().out


@pytest.mark.parametrize("script", ["applicable_rules.py", "guard_doc_routing.py"])
def test_scripts_survive_a_console_that_cannot_encode_the_rules(
    script: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 主控台與管線預設 cp950，小節標題的 ⭐ 曾讓腳本當掉（一條規則都沒印）。"""
    paths = ["--paths", "ToukaProxy/src/main.py", "--print-rules"]
    args = paths if script.startswith("app") else []
    monkeypatch.setenv("PYTHONIOENCODING", "cp950")
    monkeypatch.setenv("PYTHONUTF8", "0")
    result = execution_broker.run_sync(
        [sys.executable, "-B", str(SCRIPTS / script), *args],
        capture_output=True,
        check=False,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout


# ── 真實 repo ──────────────────────────────────────────────


def _tracked_real() -> list[str]:
    if not GIT:
        pytest.skip("git is unavailable")
    return [p for p in _git(ROOT, "ls-files", "-z").split("\0") if p]


def test_real_repo_passes_the_guard() -> None:
    assert guard.check(ROOT, _tracked_real()).errors == []


def test_real_agents_md_keeps_every_ban_within_the_read_limit() -> None:
    assert guard.last_ban_end(rules.read_lf(ROOT / "AGENTS.md")) <= guard.CODEX_READ_LIMIT_BYTES


@pytest.mark.parametrize(
    ("path", "expected_modules", "must_list", "must_not_list"),
    [
        (
            "ToukaChat/src/components/LeftSidebar.tsx",
            ["chat"],
            ["禁令 9", "禁令 13", "禁令 45", "禁令 49", "禁令 50", "禁令 51", "禁令 56", "禁令 58"],
            ["禁令 2", "禁令 33", "禁令 53"],
        ),
        (
            "ToukaMemory/src/memory_server/services/mission_manager.py",
            ["memory"],
            [
                "禁令 1",
                "禁令 6",
                "禁令 8",
                "禁令 11",
                "禁令 14",
                "禁令 29",
                "禁令 30",
                "禁令 35",
                "禁令 37",
            ],
            ["禁令 9", "禁令 33", "禁令 53", "禁令 56"],
        ),
        (
            "ToukaDispatch/server.py",
            ["dispatch"],
            ["禁令 8", "禁令 11", "禁令 29", "禁令 33", "禁令 37"],
            ["禁令 9", "禁令 35", "禁令 53"],
        ),
        (
            "uv.lock",
            ["deploy"],
            ["禁令 47", "禁令 53"],
            ["禁令 9", "禁令 33"],
        ),
    ],
)
def test_real_routing_matches_the_approved_inventory(
    path: str, expected_modules: list[str], must_list: list[str], must_not_list: list[str]
) -> None:
    route = _route(ROOT, path)
    assert route.unknown == []
    assert route.modules == expected_modules
    for key in must_list:
        assert key in _keys(route), key
    for key in must_not_list:
        assert key not in _keys(route), key


@pytest.mark.parametrize(
    ("path", "keys"),
    [
        ("ToukaProxy/src/services/mcp_hub_service.py", ["禁令 66"]),
        ("ToukaProxy/config/mcp_server_isolation.json.example", ["禁令 66"]),
        ("ToukaChat/src/components/chat/ToolApprovalCard.tsx", ["禁令 64", "禁令 66"]),
        ("ToukaChat/src/components/settings/tabs/mcp/McpIsolationSection.tsx", ["禁令 66"]),
        ("tests/lint/test_security_kernel_ratchet.py", ["禁令 66"]),
        ("ToukaProxy/src/services/approval_flow.py", ["禁令 64"]),
        ("ToukaProxy/src/routers/heartbeat.py", ["禁令 64"]),
        ("ToukaIDE/src/mcp/humanApproval.ts", ["禁令 64"]),
        (
            "ToukaMemory/src/memory_server/tools/mission_tools.py",
            ["禁令 29", "禁令 30", "禁令 31", "禁令 32", "禁令 35"],
        ),
        ("ToukaProxy/src/tools/mission_tools.py", ["禁令 29", "禁令 30", "禁令 31", "禁令 32"]),
        ("ToukaProxy/src/handlers/react_core.py", ["禁令 5", "禁令 59"]),
        ("ToukaProxy/scripts/manage_backup.py", ["禁令 68"]),
        ("ToukaChat/scripts/guard_fetch.ts", ["禁令 45"]),
        ("ToukaChat/eslint.config.mjs", ["禁令 50"]),
        ("scripts/lint_ssot.py", ["禁令 37"]),
        ("scripts/guard_creation_sovereignty.py", ["禁令 52"]),
        ("scripts/guard_module_boundaries.py", ["禁令 14"]),
        ("ToukaChat/src/components/settings/tabs/HooksTab.tsx", ["禁令 67"]),
        ("ToukaChat/src/components/settings/tabs/hooks/hook_draft.ts", ["禁令 67"]),
        ("ToukaProxy/src/routers/config.py", ["禁令 67"]),
        ("ToukaProxy/src/utils/hard_floor.py", ["禁令 67"]),
        ("ToukaProxy/src/tools/project_tools.py", ["禁令 67"]),
        ("ToukaMemory/src/memory_server/tools/core_file_ops.py", ["禁令 67"]),
    ],
)
def test_real_routing_lists_rules_for_the_files_they_govern(path: str, keys: list[str]) -> None:
    listed = _keys(rules.route([path], rules.load_config(ROOT), rules.parse_tags(ROOT)))
    assert [key for key in keys if key not in listed] == []


def test_real_hook_ban_lives_in_its_own_rule_file() -> None:
    config = rules.load_config(ROOT)
    assert config["modules"]["hooks"]["rules_file"] == "docs/rules/hooks.md"
    assert config["inline_rules"] == []


def test_real_code_that_fires_hooks_is_routed_to_the_hook_ban() -> None:
    config, tags = rules.load_config(ROOT), rules.parse_tags(ROOT)
    callers = [
        path
        for path in _tracked_real()
        if path.startswith(("ToukaProxy/src/", "ToukaMemory/src/"))
        and path.endswith(".py")
        and "services.hooks" in (ROOT / path).read_text(encoding="utf-8")
    ]
    assert callers
    assert [p for p in callers if "禁令 67" not in _keys(rules.route([p], config, tags))] == []


@pytest.mark.parametrize(
    ("path", "head"),
    [
        ("ToukaProxy/src/handlers/claude_handler.py", "4. 角色交替"),
        ("ToukaProxy/src/handlers/react_core.py", "13. 自主預算"),
        ("ToukaProxy/src/routers/heartbeat.py", "13. 自主預算"),
        ("ToukaProxy/src/services/approval_flow.py", "14. 策略即程式碼"),
        ("ToukaHeartbeat/storage_service.py", "11. 系統穩定性"),
        ("ToukaDispatch/server.py", "11. 系統穩定性"),
        ("ToukaMemory/src/memory_server/tools/mission_tools.py", "13.5 工作分派"),
        ("ToukaChat/src/components/chat/ToolApprovalCard.tsx", "2. 身分憑證化"),
    ],
)
def test_real_routing_lists_sections_for_the_files_they_govern(path: str, head: str) -> None:
    listed = _keys(rules.route([path], rules.load_config(ROOT), rules.parse_tags(ROOT)))
    assert any(key.startswith(head) for key in listed)


def test_widened_safety_rules_cover_every_proxy_file() -> None:
    """ADR-0296 決定 1：這些規則放寬到整個 Proxy，新檔案也要列出來。"""
    path = "ToukaProxy/src/services/brand_new_file.py"
    listed = _keys(rules.route([path], rules.load_config(ROOT), rules.parse_tags(ROOT)))
    assert {"禁令 60", "禁令 64", "禁令 65", "禁令 66", "禁令 68"} <= set(listed)
    assert any(key.startswith("2. 身分憑證化") for key in listed)


NAMED_FILE = re.compile(r"[\w./-]+\.(?:py|tsx?|mjs|json|yml|yaml|toml|example)\b")


def test_files_named_in_a_rule_are_covered_by_its_paths() -> None:
    """規則全文點名的檔案，改它時一定要列出這條規則。`guard_all.py` 只是守衛總入口，不算。"""
    tracked = _tracked_real()
    config, tags = rules.load_config(ROOT), rules.parse_tags(ROOT)
    misses = []
    for key, tag, globs in rules.all_rules(config, tags):
        if not isinstance(tag, rules.Tag):
            continue
        text = rules.read_lf(ROOT / (tag.file or "AGENTS.md"))
        number = key.removeprefix("禁令 ")
        body = (
            rules._ban_text(text, int(number))
            if number.isdigit()
            else rules._section_text(text, key)
        )
        for name in set(NAMED_FILE.findall(body or "")) - {"guard_all.py"}:
            hits = [p for p in tracked if p == name or p.endswith("/" + name)]
            if len(hits) == 1 and not any(rules.matches(g, hits[0]) for g in globs):
                misses.append(f"{key}: {hits[0]}")
    assert sorted(misses) == []


def test_real_docs_change_lists_nothing_extra() -> None:
    route = _route(ROOT, "docs/architecture/SSOT_MASTER_MAP.md", "README.md")
    assert route.items == []
    assert route.unknown == []


def test_guard_is_registered_in_guard_all() -> None:
    source = (SCRIPTS / "guard_all.py").read_text(encoding="utf-8")
    assert "scripts/guard_doc_routing.py" in source
    assert "[Guard #46]" in source
