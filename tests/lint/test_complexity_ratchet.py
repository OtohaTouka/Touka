"""函式複雜度只減不增（ADR-0331）。

五個產品資料夾裡，ruff C901 算出超過上限 15 的函式記在 `tests/lint/complexity_baseline.json`。
新超標或變複雜要拆；變簡單、改名或搬檔要在同一個 PR 跑 `--update`，改動才看得到。
收集用 `--isolated --ignore-noqa`，`ruff.toml` 的 per-file-ignores 與 `# noqa` 都壓不掉。
"""

from __future__ import annotations

import json
from collections import Counter
from subprocess import CompletedProcess
from typing import TYPE_CHECKING

import lint_ratchet  # scripts/ 由 tests/conftest.py 加進 sys.path
import pytest
from lint_ratchet import (
    COMPLEXITY_ARGS,
    COMPLEXITY_BASELINE,
    MAX_COMPLEXITY,
    ONE_WAY_TOOLS,
    PRODUCT_SOURCE_ROOTS,
    TOOLS,
    collect_complexity,
    compare_complexity,
    load_baseline,
    main,
    parse_complexity,
)

if TYPE_CHECKING:
    from pathlib import Path

BACKSLASH = chr(92)


def _branches(count: int, indent: int) -> str:
    """`count` 個 if 加一個 return：複雜度是 count + 1。"""
    pad = " " * indent
    ifs = "".join(f"{pad}if x == {i}:\n{pad}    return {i}\n" for i in range(count))
    return ifs + f"{pad}return -1\n"


def _mini_project(root: Path) -> None:
    """真的 ruff 要掃的小專案：每種邊界各一個。"""
    source = (
        "FLAG = True\n\n\n"
        "def decorate(_label):\n    return lambda f: f\n\n\n"
        "def at_limit(x):\n" + _branches(14, 4) + "\n\n"
        "def over_limit(x):\n" + _branches(15, 4) + "\n\n"
        "class First:\n    def run(self, x):\n" + _branches(15, 8) + "\n\n"
        "class Second:\n    def run(self, x):\n" + _branches(16, 8) + "\n\n"
        "def outer(x):\n    def inner(x):\n" + _branches(15, 8) + "    return inner(x)\n\n\n"
        "def factory():\n    class Inner:\n        def method(self, x):\n"
        + _branches(15, 12) + "    return Inner\n\n\n"
        '@decorate(\n    "multi-line",\n)\ndef decorated(x):\n' + _branches(15, 4) + "\n\n"
        "async " + BACKSLASH + "\ndef spaced(x):\n" + _branches(15, 4) + "\n\n"
        "def silenced(x):  # noqa: C901\n" + _branches(15, 4) + "\n\n"
        "if FLAG:\n    def twin(x):\n" + _branches(15, 8)
        + "else:\n    def twin(x):\n" + _branches(15, 8)
        # 只有 setter 超標：照在檔內的順序它是第二個，互換 getter、setter 才會紅。
        + "\n\nclass Prop:\n    @property\n    def value(self):\n        return 1\n\n"
        "    @value.setter\n    def value(self, x):\n" + _branches(15, 8)
    )
    deep = "def deep(x):\n" + _branches(15, 4)
    for folder in ("sub", "dist"):
        (root / "pkg" / folder).mkdir(parents=True)
    (root / "pkg" / "a.py").write_text(source, encoding="utf-8")
    # 藏檔的各種寫法：預設排除的資料夾、.pyw、.gitignore、檔頭 noqa，都要掃到。
    for name in ("sub/b.py", "dist/built.py", "gui.pyw", "hidden.py"):
        (root / "pkg" / name).write_text(deep, encoding="utf-8")
    (root / "pkg" / "quiet.py").write_text("# ruff: noqa\n" + deep, encoding="utf-8")
    (root / ".git").mkdir()  # 有 .git 時 ruff 預設會照 .gitignore 跳過
    (root / ".gitignore").write_text("pkg/hidden.py\n", encoding="utf-8")
    # 小專案自帶的設定想把 C901 關掉；--isolated 不讀它。
    (root / "ruff.toml").write_text(
        '[lint.per-file-ignores]\n"pkg/**/*.py" = ["C901"]\n', encoding="utf-8"
    )


def _item(root: Path, name: str, score: int, row: int, *, code: str = "C901") -> dict:
    return {
        "code": code,
        "filename": str(root / "pkg" / "a.py"),
        "location": {"row": row, "column": 5},
        "message": f"`{name}` is too complex ({score} > {MAX_COMPLEXITY})",
    }


def _result(items: list[dict] | str, returncode: int = 1, stderr: str = "") -> CompletedProcess:
    stdout = items if isinstance(items, str) else json.dumps(items)
    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ── 真的 repo ───────────────────────────────────────────────────────────────


def test_complexity_matches_the_baseline() -> None:
    problems = compare_complexity(collect_complexity(), load_baseline(COMPLEXITY_BASELINE))
    assert not problems, "\n".join(problems)


def test_the_limit_and_the_ruff_arguments_are_pinned() -> None:
    assert MAX_COMPLEXITY == 15
    assert COMPLEXITY_ARGS == (
        "ruff", "check", "--isolated", "--ignore-noqa", "--no-cache", "--no-respect-gitignore",
        "--select", "C901", "--config", "lint.mccabe.max-complexity=15",
        "--config", "exclude=[]", "--config", 'extend-include=["*.pyw"]',
        "--output-format", "json",
    )
    for loophole in ("--ignore", "--exclude", "--extend-exclude", "--per-file-ignores"):
        assert loophole not in COMPLEXITY_ARGS


def test_it_scans_the_product_roots_and_is_a_two_way_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake(args: list[str], cwd: object = None) -> CompletedProcess:
        seen.append(args)
        return _result([], returncode=0)

    monkeypatch.setattr(lint_ratchet, "_run_tool_module", fake)
    assert collect_complexity() == Counter()
    assert seen == [[*COMPLEXITY_ARGS, *PRODUCT_SOURCE_ROOTS]]
    assert TOOLS["complexity"] == (collect_complexity, COMPLEXITY_BASELINE)
    assert "complexity" not in ONE_WAY_TOOLS


# ── 用真的 ruff 跑小專案 ─────────────────────────────────────────────────────


def test_real_ruff_on_a_mini_project(tmp_path: Path) -> None:
    _mini_project(tmp_path)
    assert collect_complexity(tmp_path, ("pkg",)) == Counter({
        "pkg/a.py::over_limit": 16,
        "pkg/a.py::First.run": 16,
        "pkg/a.py::Second.run": 17,
        "pkg/a.py::outer": 17,  # 巢狀函式的分支算進外層
        "pkg/a.py::outer.<locals>.inner": 16,
        "pkg/a.py::factory": 17,  # 巢狀類別的方法也算進外層
        "pkg/a.py::factory.<locals>.Inner.method": 16,
        "pkg/a.py::decorated": 16,
        "pkg/a.py::spaced": 16,
        "pkg/a.py::silenced": 16,  # def 行的 noqa 壓不掉
        "pkg/a.py::twin": 16,
        "pkg/a.py::twin#2": 16,
        "pkg/a.py::Prop.value#2": 16,
        "pkg/sub/b.py::deep": 16,  # 路徑一律用 /
        "pkg/dist/built.py::deep": 16,
        "pkg/gui.pyw::deep": 16,
        "pkg/hidden.py::deep": 16,
        "pkg/quiet.py::deep": 16,
    })


def test_a_missing_source_root_fails(tmp_path: Path) -> None:
    _mini_project(tmp_path)
    with pytest.raises(RuntimeError, match="missing"):
        collect_complexity(tmp_path, ("pkg", "gone"))


# ── 解析：看不懂就丟錯，不當成 0 筆 ──────────────────────────────────────────


def test_parse_reads_names_by_definition(tmp_path: Path) -> None:
    _mini_project(tmp_path)
    found = parse_complexity(_result([_item(tmp_path, "run", 17, 110)]), tmp_path)
    assert found == Counter({"pkg/a.py::Second.run": 17})


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (_result([], returncode=0, stderr="warning: something"), "stderr"),
        (_result([], returncode=2), "exit 2"),
        (_result("not json"), "JSON"),
        (_result([], returncode=1), "exit 1"),
    ],
    ids=["stderr", "exit-2", "not-json", "exit-1-without-results"],
)
def test_parse_refuses_a_broken_run(tmp_path: Path, result: CompletedProcess, reason: str) -> None:
    with pytest.raises(RuntimeError, match=reason):
        parse_complexity(result, tmp_path)


def test_parse_refuses_results_with_exit_0(tmp_path: Path) -> None:
    _mini_project(tmp_path)
    with pytest.raises(RuntimeError, match="exit 0"):
        parse_complexity(_result([_item(tmp_path, "over_limit", 16, 40)], 0), tmp_path)


@pytest.mark.parametrize(
    "item",
    [
        {"code": "invalid-syntax", "message": "SyntaxError: bad"},
        {"code": None, "message": "SyntaxError: bad"},
        {"message": "`over_limit` has 16 branches"},
        {"message": "`over_limit` is too complex (16 > 10)"},
    ],
    ids=["invalid-syntax", "no-code", "unknown-message", "other-limit"],
)
def test_parse_refuses_what_it_does_not_understand(tmp_path: Path, item: dict) -> None:
    _mini_project(tmp_path)
    with pytest.raises(RuntimeError):
        parse_complexity(_result([{**_item(tmp_path, "over_limit", 16, 40), **item}]), tmp_path)


@pytest.mark.parametrize(("name", "row"), [("vanished", 40), ("over_limit", 3)])
def test_parse_refuses_a_name_it_cannot_find(tmp_path: Path, name: str, row: int) -> None:
    _mini_project(tmp_path)
    with pytest.raises(RuntimeError, match="definition"):
        parse_complexity(_result([_item(tmp_path, name, 16, row)]), tmp_path)


# ── 比對與命令列 ─────────────────────────────────────────────────────────────


def test_compare_reports_both_directions_with_different_advice() -> None:
    baseline = {"a.py::frozen": 20, "a.py::shrunk": 20, "a.py::moved": 18}
    current = Counter({"a.py::frozen": 21, "a.py::shrunk": 19, "b.py::new": 16})
    problems = compare_complexity(current, baseline)
    assert len(problems) == 4
    by_key = {next(key for key in (*baseline, *current) if key in p): p for p in problems}
    assert "要拆" in by_key["a.py::frozen"] and "--update" not in by_key["a.py::frozen"]
    assert "要拆" in by_key["b.py::new"] and "--update" not in by_key["b.py::new"]
    assert "--update" in by_key["a.py::shrunk"] and "要拆" not in by_key["a.py::shrunk"]
    assert "--update" in by_key["a.py::moved"] and "要拆" not in by_key["a.py::moved"]
    assert compare_complexity(Counter(baseline), baseline) == []


@pytest.mark.parametrize("flags", [[], ["--update"]], ids=["check", "update"])
def test_an_empty_result_against_a_non_empty_baseline_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    """收集壞掉時不能被一次 --update 寫成空基準，等於把檢查關掉。"""
    baseline = tmp_path / "complexity_baseline.json"
    baseline.write_text('{\n  "a.py::frozen": 20\n}\n', encoding="utf-8")
    monkeypatch.setitem(lint_ratchet.TOOLS, "complexity", (Counter, baseline))
    with pytest.raises(RuntimeError, match="{}"):
        main(["complexity", *flags])
    assert json.loads(baseline.read_text(encoding="utf-8")) == {"a.py::frozen": 20}


def test_update_writes_a_sorted_baseline_that_then_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "complexity_baseline.json"
    current = Counter({"b.py::second": 17, "a.py::first": 16})
    monkeypatch.setitem(lint_ratchet.TOOLS, "complexity", (lambda: current, baseline))
    assert main(["complexity", "--update"]) == 0
    assert list(json.loads(baseline.read_text(encoding="utf-8"))) == ["a.py::first", "b.py::second"]
    assert main(["complexity"]) == 0
    assert "PASS" in capsys.readouterr().out
    grown = Counter({**current, "c.py::third": 16})
    monkeypatch.setitem(lint_ratchet.TOOLS, "complexity", (lambda: grown, baseline))
    assert main(["complexity"]) == 1
