"""ruff 挑檔時不能漏掉 git 會收的 Python 檔（ADR-0278「檔案不能藏起來」）。

三道 ruff 檢查（完整 ruff 棘輪、CI 的 F 步驟、Guard #44）都照 ruff 的預設挑檔案：
`.gitignore`、`.ignore`、`.git/info/exclude` 列到的、`dist`、`venv` 這類資料夾裡的、
連結底下的都不看，但這些檔照樣會被執行。這裡比對「git 會收的檔」和「ruff 看到的檔」，
再把 `ruff.toml` 挑檔的設定和三道指令整組釘住，從設定或參數藏檔也會紅。
"""

from __future__ import annotations

import copy
import shutil
import sys
import tomllib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import guard_ruff_banned_api  # scripts/ 由 tests/conftest.py 加進 sys.path
import lint_ratchet
import pytest
from lint_ratchet import (
    PRODUCT_SOURCE_ROOTS,
    PROJECT_ROOT,
    RUFF_CHECK_ARGS,
    _git,
    _run_tool_module,
    collect_ruff,
    unchecked_python_files,
)

if TYPE_CHECKING:
    from pathlib import Path

RUFF_TOML = PROJECT_ROOT / "ruff.toml"
# ruff.toml 最上層只准有這幾項；`extend`、`include`、`exclude`、`respect-gitignore`
# 和以後新增的鍵都會改變挑檔，要加先改這裡並在 PR 說明理由。
ALLOWED_TOP_LEVEL = {"target-version", "line-length", "extend-include", "lint"}
SCOPES = {
    "product-roots": PRODUCT_SOURCE_ROOTS,
    "guard44-scan-dirs": tuple(guard_ruff_banned_api.SCAN_DIRS),
}
HIDDEN_HEADER = "這些 git 會收的 Python 檔，ruff 挑檔時沒看到，三道 ruff 檢查都不會檢查它們："
FIX_HINT = (
    "修法：拿掉列到它的 .gitignore／.ignore／.git/info/exclude 規則，或把檔案搬出 dist、"
    "venv、node_modules 這類資料夾；副檔名一律小寫。"
)


def _selection_problems(config: dict[str, Any]) -> list[str]:
    """ruff.toml 裡會改變挑檔的設定；空清單代表和允許的一樣。"""
    problems = []
    if set(config) != ALLOWED_TOP_LEVEL:
        problems.append(f"最上層只准有 {sorted(ALLOWED_TOP_LEVEL)}，現在是 {sorted(config)}")
    if config.get("extend-include") != ["*.pyw"]:
        problems.append(f"extend-include 要是 ['*.pyw']，現在是 {config.get('extend-include')}")
    excludes = sorted(key for key in config.get("lint", {}) if "exclude" in key)
    if excludes:
        problems.append(f"[lint] 不准有 {excludes}")
    return problems


@pytest.mark.parametrize("roots", list(SCOPES.values()), ids=list(SCOPES))
def test_ruff_sees_every_python_file_git_would_commit(roots: tuple[str, ...]) -> None:
    hidden = unchecked_python_files(roots)
    assert hidden == [], "\n".join([HIDDEN_HEADER, *hidden, FIX_HINT])


def test_ruff_toml_does_not_change_which_files_ruff_picks() -> None:
    config = tomllib.loads(RUFF_TOML.read_text(encoding="utf-8"))
    assert _selection_problems(config) == []


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        (None, "extend", "base.toml"),
        (None, "exclude", ["src/hidden.py"]),
        (None, "extend-exclude", ["src/hidden.py"]),
        (None, "respect-gitignore", False),
        (None, "extend-include", ["*.pyw", "*.py3"]),
        ("lint", "exclude", ["src/hidden.py"]),
    ],
)
def test_selection_pin_rejects_settings_that_hide_files(
    section: str | None, key: str, value: object
) -> None:
    config = copy.deepcopy(tomllib.loads(RUFF_TOML.read_text(encoding="utf-8")))
    (config[section] if section else config)[key] = value
    assert _selection_problems(config)


def test_shared_ruff_args_are_pinned_literally() -> None:
    """其他釘子的期望值都從這個常數推出來；常數和 CI 一起改時，只有這裡會紅。"""
    assert RUFF_CHECK_ARGS == ("ruff", "check", "--no-cache", "--config", "ruff.toml")


def test_collect_ruff_runs_exactly_the_shared_selection_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(lint_ratchet, "subprocess", SimpleNamespace(run=run))
    collect_ruff()
    expected = [*RUFF_CHECK_ARGS, "--output-format", "json", "--exit-zero", *PRODUCT_SOURCE_ROOTS]
    assert calls == [[sys.executable, "-m", *expected]]


def test_guard44_runs_exactly_this_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(guard_ruff_banned_api, "subprocess", SimpleNamespace(run=run))
    assert guard_ruff_banned_api.run_guard()[0]
    root = guard_ruff_banned_api.PROJECT_ROOT
    targets = [str(root / name) for name in guard_ruff_banned_api.SCAN_DIRS]
    expected = ["ruff", "check", *targets, "--select", "TID251"]
    assert calls == [[*expected, "--config", str(root / "ruff.toml"), "--output-format", "concise"]]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """暫存小 repo，git 和 ruff 都讀不到使用者的全域設定與全域 gitignore。"""
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_COUNT",  # `git -c`／hook 帶進來的設定（例如 core.excludesFile）
        "GIT_CONFIG_PARAMETERS",
    ):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    empty_config = home / "empty.gitconfig"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    # Windows 上的 ruff 用 USERPROFILE 找全域設定，不看 HOME。
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    # 放在子資料夾：工作資料夾等於天花板本身時，git 仍會往上找。
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    _git(["init", "-q"], root)
    shutil.copyfile(RUFF_TOML, root / "ruff.toml")
    return root


def _write(root: Path, *names: str, content: str = "x = 1\n") -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _track(root: Path, *names: str) -> None:
    _write(root, *names)
    _git(["add", "-f", "--", *names], root)


def test_files_hidden_from_ruff_are_reported_and_visible_ones_are_not(repo: Path) -> None:
    _write(repo, ".gitignore", content="src/ignored_root.py\nsrc/data/\n")
    _write(repo, "src/sub/.gitignore", content="ignored_nested.py\n")
    _write(repo, "src/.ignore", content="dot_ignore.py\n")
    with (repo / ".git" / "info" / "exclude").open("a", encoding="utf-8") as exclude:
        exclude.write("src/info_exclude.py\n")
    hidden = [
        "src/ignored_root.py",
        "src/sub/ignored_nested.py",
        "src/dot_ignore.py",
        "src/info_exclude.py",
        "src/dist/a.py",
        "src/venv/b.py",
        "src/.venv/c.py",
        "src/node_modules/d.py",
        "src/upper.PY",
    ]
    visible = ["src/ok.py", "src/stub.pyi", "src/gui.pyw", "src/空 白 檔.py"]
    _track(repo, *hidden, *visible, "src/gone.py")
    (repo / "src" / "gone.py").unlink()  # 已追蹤但本機刪了：不在磁碟上就不報
    _write(repo, "src/dist/new.py")  # 還沒 add、沒被忽略：git 會收，ruff 看不到
    _write(repo, "src/data/runtime.py")  # 被忽略又沒追蹤：執行期資料，不報也不讀

    assert unchecked_python_files(["src"], repo) == sorted([*hidden, "src/dist/new.py"])


def test_pyw_counts_as_python_even_without_extend_include(repo: Path) -> None:
    (repo / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    _track(repo, "src/ok.py", "src/gui.pyw")
    assert unchecked_python_files(["src"], repo) == ["src/gui.pyw"]


def test_lint_exclude_skips_checks_but_not_show_files_so_the_config_pin_is_needed(
    repo: Path,
) -> None:
    """`[lint] exclude` 的檔 --show-files 照樣列，比對抓不到，只能靠設定釘子擋。"""
    _write(repo, "src/hidden.py", content="import os\n")  # F401
    _git(["add", "--", "src/hidden.py"], repo)
    check = [*RUFF_CHECK_ARGS, "--select", "F", "--output-format", "concise", "src"]
    assert _run_tool_module(check, cwd=repo).returncode == 1
    config = repo / "ruff.toml"
    config.write_text('line-length = 100\n[lint]\nexclude = ["src/hidden.py"]\n', encoding="utf-8")
    assert _run_tool_module(check, cwd=repo).returncode == 0
    assert unchecked_python_files(["src"], repo) == []
    assert _selection_problems(tomllib.loads(config.read_text(encoding="utf-8")))


@pytest.mark.parametrize("mode", ["120000", "160000"], ids=["symlink", "submodule"])
def test_links_and_submodules_in_scope_fail(repo: Path, mode: str) -> None:
    """ruff 不跟進連結和子模組，裡面的檔就藏起來了；用 index 造，Windows 也能跑。"""
    _track(repo, "src/ok.py")
    _write(repo, "blob.txt", content="target\n")
    blob = _git(["hash-object", "-w", "blob.txt"], repo).strip()
    _git(["update-index", "--add", "--cacheinfo", f"{mode},{blob},src/entry"], repo)
    with pytest.raises(RuntimeError, match="links or submodules"):
        unchecked_python_files(["src"], repo)


def test_missing_git_fails(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _track(repo, "src/ok.py")
    monkeypatch.setattr(lint_ratchet, "shutil", SimpleNamespace(which=lambda _name: None))
    with pytest.raises(RuntimeError, match="git not found"):
        unchecked_python_files(["src"], repo)


def test_a_folder_that_is_not_a_git_repo_fails(repo: Path) -> None:
    plain = repo.parent / "plain"
    _write(plain, "src/ok.py")
    with pytest.raises(RuntimeError, match="git ls-files failed"):
        unchecked_python_files(["src"], plain)


def test_a_missing_scope_folder_fails(repo: Path) -> None:
    _track(repo, "src/ok.py")
    with pytest.raises(RuntimeError, match="source roots missing"):
        unchecked_python_files(["src", "gone"], repo)


def test_a_broken_ruff_config_fails(repo: Path) -> None:
    _track(repo, "src/ok.py")
    (repo / "ruff.toml").write_text("line-length = \n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="ruff --show-files did not finish"):
        unchecked_python_files(["src"], repo)


def test_no_python_files_in_scope_fails(repo: Path) -> None:
    _track(repo, "src/notes.txt")
    with pytest.raises(RuntimeError, match="git listed no Python files"):
        unchecked_python_files(["src"], repo)
