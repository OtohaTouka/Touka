"""後端未用的函式、方法、類別不得增加（禁令 46，ADR-0297）。

CI 本來就跑 pytest，所以這支測試就是這道檢查在 CI 上的關卡。比對是單向的：多出來才紅，
少了不紅，因為別處新增一個同名的用法就會讓項目消失，和那個 PR 無關。
"""

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import guard_dead_code
import lint_ratchet  # scripts/ 由 tests/conftest.py 加進 sys.path
import pytest
from lint_ratchet import (
    PRODUCT_SOURCE_ROOTS,
    VULTURE_BASELINE,
    VULTURE_WHITELIST,
    collect_vulture,
    compare_additions,
    load_baseline,
)

RATCHET_COMMAND = "python scripts/lint_ratchet.py vulture"
# 審查過的參數。門檻提高或多排除一種裝飾器都會讓數字悄悄變好看，所以釘在這裡。
REVIEWED_ARGUMENTS = [
    "vulture",
    *PRODUCT_SOURCE_ROOTS,
    "scripts/vulture_whitelist.py",
    "--min-confidence",
    "60",
    "--ignore-decorators",
    "@router.*,@*_router.*,@app.*,@mcp.*,@field_validator,@model_validator,"
    "@computed_field,@self.client.event",
]
TINY_SOURCE = """\
class Model:
    @field_validator("x")
    def registered_validator(cls, value):
        return value

    @classmethod
    def unused_classmethod(cls):
        return 1

    @staticmethod
    def unused_staticmethod():
        return 1

    @property
    def unused_property(self):
        return 1

    def unused_method(self):
        return 1


@router.get("/x")
def route_handler():
    return 1


@mcp.tool()
def mcp_tool():
    return 1


def unused_function():
    return 1


def name_used_elsewhere():
    return 1


class UnusedClass:
    pass


unused_variable = 1
Model().name_used_elsewhere
"""


def _fake_vulture(monkeypatch: pytest.MonkeyPatch, *, returncode: int, stdout: str = "") -> list:
    """替換子程序，回傳收到的 argv 清單，讓測試不必真的跑 vulture。"""
    calls: list = []
    done = SimpleNamespace(returncode=returncode, stdout=stdout, stderr="broken input")

    def run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return done

    monkeypatch.setattr(lint_ratchet, "subprocess", SimpleNamespace(run=run))
    return calls


def test_no_new_unused_definitions_beyond_the_baseline() -> None:
    problems = compare_additions(collect_vulture(), load_baseline(VULTURE_BASELINE))
    assert not problems, "\n".join(
        [
            *problems,
            "接上或刪掉；確認要留就跑：python scripts/lint_ratchet.py vulture --update"
            "，並在 PR 說明逐項列出。",
        ]
    )


def test_collect_uses_the_reviewed_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_vulture(monkeypatch, returncode=0)
    collect_vulture()
    assert calls[0][1:2] == ["-m"]
    assert calls[0][2:] == REVIEWED_ARGUMENTS


def test_ignored_decorators_keep_the_builtin_ones_reportable() -> None:
    """@classmethod 這類內建裝飾器不是靠註冊才用到，排除了就等於不檢查方法。"""
    ignored = lint_ratchet.VULTURE_IGNORE_DECORATORS.split(",")
    assert not {"@classmethod", "@staticmethod", "@property"} & set(ignored)


@pytest.mark.parametrize("returncode", [1, 2])
def test_collect_fails_when_vulture_does_not_finish(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    """輸入或參數壞掉不能被當成沒有死碼。"""
    _fake_vulture(monkeypatch, returncode=returncode)
    with pytest.raises(RuntimeError, match=f"exit {returncode}"):
        collect_vulture()


def test_collect_keeps_only_definitions_and_normalizes_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = "\n".join(
        [
            r"ToukaProxy\src\a.py:1: unused function 'f' (60% confidence)",
            "ToukaProxy/src/a.py:2: unused method 'm' (60% confidence, 3 lines)",
            r"ToukaHeartbeat\b.py:3: unused class 'C' (60% confidence)",
            r"ToukaHeartbeat\b.py:4: unused property 'p' (60% confidence)",
            r"ToukaHeartbeat\b.py:5: unused variable 'v' (60% confidence)",
            r"ToukaHeartbeat\b.py:6: unused attribute 'a' (60% confidence)",
            r"ToukaHeartbeat\b.py:7: unused import 'i' (90% confidence)",
            r"ToukaHeartbeat\b.py:8: unreachable code after 'return' (100% confidence)",
            r"ToukaHeartbeat\b.py:9: unsatisfiable 'if' condition (100% confidence)",
            r"ToukaHeartbeat\b.py:10: unused function 'f' (60% confidence)",
            r"ToukaHeartbeat\b.py:11: unused function 'f' (60% confidence)",
        ]
    )
    _fake_vulture(monkeypatch, returncode=3, stdout=stdout)
    assert collect_vulture() == Counter(
        {
            "ToukaProxy/src/a.py:function:f": 1,
            "ToukaProxy/src/a.py:method:m": 1,
            "ToukaHeartbeat/b.py:class:C": 1,
            "ToukaHeartbeat/b.py:property:p": 1,
            "ToukaHeartbeat/b.py:function:f": 2,
        }
    )


@pytest.mark.parametrize(
    "line",
    [
        "ToukaProxy/src/a.py:1: dead function 'f' (60% confidence)",
        "something else entirely",
    ],
)
def test_collect_fails_on_output_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    """升級後格式變了，全部比對不到會變成 0 筆而通過，所以看不懂就失敗。"""
    _fake_vulture(monkeypatch, returncode=3, stdout=line)
    with pytest.raises(RuntimeError, match="unexpected vulture output"):
        collect_vulture()


def test_real_vulture_reports_the_expected_definitions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """用真的 vulture 和審查過的參數跑一個小專案：靠註冊才用到的不報，名字和別處相同的也不報。"""
    for root in PRODUCT_SOURCE_ROOTS:
        (tmp_path / root).mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / VULTURE_WHITELIST).write_text("", encoding="utf-8")
    (tmp_path / "ToukaProxy/src/sample.py").write_text(TINY_SOURCE, encoding="utf-8")
    monkeypatch.setattr(lint_ratchet, "PROJECT_ROOT", tmp_path)

    prefix = "ToukaProxy/src/sample.py"
    assert set(collect_vulture()) == {
        f"{prefix}:method:unused_classmethod",
        f"{prefix}:method:unused_staticmethod",
        f"{prefix}:method:unused_method",
        f"{prefix}:property:unused_property",
        f"{prefix}:function:unused_function",
        f"{prefix}:class:UnusedClass",
    }


def test_real_vulture_reports_a_broken_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """資料夾不見時要失敗，不能當成沒有死碼。"""
    (tmp_path / "scripts").mkdir()
    (tmp_path / VULTURE_WHITELIST).write_text("", encoding="utf-8")
    monkeypatch.setattr(lint_ratchet, "PROJECT_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="exit 1"):
        collect_vulture()


def test_compare_additions_is_one_way() -> None:
    baseline = {"a.py:function:f": 1, "a.py:method:m": 2}
    assert compare_additions(Counter(baseline), baseline) == []
    assert compare_additions(Counter({"a.py:function:f": 1}), baseline) == []

    new_key = compare_additions(Counter({**baseline, "b.py:class:C": 1}), baseline)
    assert len(new_key) == 1 and "b.py:class:C" in new_key[0] and "變多" in new_key[0]

    more = compare_additions(Counter({**baseline, "a.py:function:f": 2}), baseline)
    assert len(more) == 1 and "a.py:function:f" in more[0] and "變多" in more[0]


def test_main_passes_on_fewer_items_and_fails_on_new_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = tmp_path / "baseline.json"
    counts = Counter({"b.py:method:m": 1, "a.py:function:f": 1})
    monkeypatch.setattr(lint_ratchet, "TOOLS", {"vulture": (lambda: counts, baseline)})
    assert lint_ratchet.main(["vulture", "--update"]) == 0
    assert list(json.loads(baseline.read_text(encoding="utf-8"))) == [
        "a.py:function:f",
        "b.py:method:m",
    ]
    assert lint_ratchet.main(["vulture"]) == 0

    del counts["a.py:function:f"]
    assert lint_ratchet.main(["vulture"]) == 0

    counts["c.py:class:C"] = 1
    assert lint_ratchet.main(["vulture"]) == 1


def test_baseline_keys_are_repo_relative_with_forward_slashes() -> None:
    """只看格式：檔案拿掉後舊鍵留著不算問題（少了不紅）。"""
    keys = load_baseline(VULTURE_BASELINE)
    assert keys
    for key in keys:
        path, kind, name = key.rsplit(":", 2)
        assert "\\" not in path and not Path(path).is_absolute()
        assert kind in lint_ratchet.VULTURE_KINDS and name


def _run_dead_code_guard(
    monkeypatch: pytest.MonkeyPatch, failing_command: str = ""
) -> tuple[list[str], int]:
    """替換守衛跑命令的入口，回傳它要跑的命令與結束碼。"""
    commands: list[str] = []

    def fake_run(command: str, _cwd: Path, _name: str) -> tuple[bool, str]:
        commands.append(command)
        return command != failing_command, ""

    monkeypatch.setattr(guard_dead_code, "run", fake_run)
    with pytest.raises(SystemExit) as exited:
        guard_dead_code.main()
    return commands, exited.value.code


def test_dead_code_guard_runs_the_ratchet_and_scans_all_four_folders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands, exit_code = _run_dead_code_guard(monkeypatch)
    assert exit_code == 0
    assert RATCHET_COMMAND in commands
    high_confidence = [command for command in commands if "--min-confidence 80" in command]
    assert len(high_confidence) == 1
    assert all(root in high_confidence[0] for root in PRODUCT_SOURCE_ROOTS)


def test_dead_code_guard_fails_when_the_ratchet_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _, exit_code = _run_dead_code_guard(monkeypatch, failing_command=RATCHET_COMMAND)
    assert exit_code == 1
