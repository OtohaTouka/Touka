"""條目自己的環境變數 ``exec_env``（C2-3 設計 §2.6）：啟動器套完限制後才交給目標程式.

- spec 的選用鍵 ``exec_env``：物件；名稱 ``[A-Za-z_][A-Za-z0-9_]{0,127}``；值是字串、沒有 NUL、
  編得成檔案系統編碼；合計 ≤64 KiB、≤256 個；profile 管的名稱（HOME、TMP*、XDG_*、git 那組、
  ``PYTEST_ADDOPTS``、``TOUKA_EXEC_ID``、快取變數；不分大小寫）拒收。不合格一律 ``spec_invalid``。
- 直譯器控制與動態連結 loader 的變數（禁令 19，和 ``strip_sensitive_env`` 同一套黑名單模式加顯式
  移除清單，密鑰那一組除外）移除、目標拿不到；Hub 範本條目要的名稱一個都不受影響。
- 不認得 ``exec_env`` 的舊啟動器（``/opt`` 還沒重建）以 ``spec_invalid`` 拒絕、指令不執行。
- 執行器 START 的 env 仍只收精確名稱允許清單（多了 ``UV_PYTHON_DOWNLOADS``）；``exec_env`` 走 spec。

任何平台都能跑；真核心的案例在 ``test_isolation_linux.py``。
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import textwrap
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
from src.sandbox_kernel import launcher
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation import executor_session as es
from src.services.isolation import profiles

if TYPE_CHECKING:
    from collections.abc import Callable

sp = launcher.spec
ENTRY_ENV = {"U3A_ENTRY_TOKEN": "entry-secret", "U3A_SHARED": "entry", "NOTION_TOKEN": "n"}
# 禁令 19 那一套裡目標拿不到的（大小寫變體、子字串比對同 strip_sensitive_env）
STRIPPED_ENV = {
    "LD_PRELOAD": "/srv/x.so", "ld_audit": "/srv/a.so", "LD_LIBRARY_PATH": "/srv",
    "PYTHONPATH": "/srv", "PYTHONHOME": "/srv", "PythonStartup": "/srv/s.py",
    "NODE_OPTIONS": "--require /srv/x.js", "DYLD_INSERT_LIBRARIES": "/srv/x.dylib",
    "JAVA_TOOL_OPTIONS": "-javaagent:/srv/a.jar", "PERL5LIB": "/srv", "GEM_PATH": "/srv",
    "BUILD_DIR": "/srv",  # 含「LD_」：和演化沙箱一樣以子字串比對
}
# strip_sensitive_env 的密鑰名稱那一組只用在演化沙箱繼承 proxy 環境時；條目 env 本來就要把伺服器
# 自己的 token 交給它（ADR-0254），所以 exec_env 不套用
CREDENTIAL_PATTERNS = frozenset({"SECRET", "KEY", "TOKEN", "AUTH", "CREDENTIAL", "PASSWORD"})
HUB_EXAMPLE = Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "mcp_hub.json.example"
# C2-3 之前的啟動器認得的頂層欄位（``/opt`` 的舊 kernel）
PRE_C2_3_TOP_KEYS = frozenset(
    {"version", "profile", "rules", "block_ports", "rlimits", "pin", "parent_pid", "net_strict"}
)


def _document(**fields: object) -> dict:
    document: dict = {"version": sp.SPEC_VERSION, "profile": "mcp_self", "block_ports": []}
    document.update(fields)
    return document


def _parse(document: dict) -> sp.LaunchSpec:
    return sp.parse_spec(json.dumps(document).encode("utf-8"))


def _code(document: dict) -> str:
    with pytest.raises(sp.SpecError) as caught:
        _parse(document)
    return caught.value.code


# ── spec 的格式 ─────────────────────────────────────────────────────────────────────


def test_exec_env_is_optional_and_keeps_every_entry():
    assert _parse(_document()).exec_env == ()
    entries = {"PATH": "/usr/bin", "_x": "", "A" * 128: "v", "TOKEN": "é" * 3, **ENTRY_ENV}
    parsed = _parse(_document(exec_env=entries))
    assert dict(parsed.exec_env) == entries
    assert parsed.exec_env == tuple(sorted(entries.items()))


@pytest.mark.parametrize(
    "exec_env",
    [
        [], "A=1", None, 1,
        {"": "x"}, {"1A": "x"}, {"A-B": "x"}, {"A B": "x"}, {"A=B": "x"}, {"Ä": "x"},
        {"A\n": "x"}, {"A" * 129: "x"},
        {"A": 1}, {"A": None}, {"A": ["x"]}, {"A": True}, {"A": "a\x00b"}, {"A": "\ud800"},
    ],
    ids=[
        "list", "string", "null", "number",
        "empty-name", "digit-first", "dash", "space", "equals", "non-ascii", "newline",
        "long-name", "int-value", "null-value", "list-value", "bool-value", "nul", "lone-surrogate",
    ],
)
def test_exec_env_rejects_malformed_names_and_values(exec_env):
    assert _code(_document(exec_env=exec_env)) == "spec_invalid"


@pytest.mark.parametrize(
    "name",
    [
        "HOME", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0", "GIT_CONFIG_GLOBAL", "GIT_CEILING_DIRECTORIES", "PYTEST_ADDOPTS",
        "TOUKA_EXEC_ID", "UV_CACHE_DIR", "UV_TOOL_DIR", "HF_HOME", "PIP_CACHE_DIR",
        "npm_config_cache", "NPM_CONFIG_PREFIX", "Npm_Config_Cache", "home", "xdg_config_home",
    ],
)
def test_exec_env_rejects_names_the_profile_owns(name):
    """條目的 env 不能蓋 profile 管的變數（否則伺服器會去寫 Landlock 沒給的地方）."""
    assert sp.exec_env_reserved(name) is True
    assert _code(_document(exec_env={"PATH": "/usr/bin", name: "/srv/elsewhere"})) == "spec_invalid"


@pytest.mark.parametrize("name", ["PATH", "LANG", "HOMEPAGE", "XDGX", "GIT_DIR", "TOUKA_EXEC_IDS"])
def test_names_that_only_look_alike_are_not_reserved(name):
    assert sp.exec_env_reserved(name) is False
    assert dict(_parse(_document(exec_env={name: "v"})).exec_env) == {name: "v"}


def test_reserved_names_cover_everything_the_profile_and_executor_set():
    """profile 設的（``sandbox_env``）、拿掉的快取變數與執行器給的 exec id 全部是保留名稱.

    kernel 不能匯入 profiles，名單各存一份；這裡釘住兩邊不漂移。
    """
    home, tmp = PurePosixPath("/st/home"), PurePosixPath("/st/tmp")
    managed = profiles.sandbox_env(home, tmp, [PurePosixPath("/srv")], PurePosixPath("/srv/p"))
    names = {*managed, *profiles.SANDBOX_ENV_UNSET, es.EXEC_ID_ENV}
    assert {name for name in names if not sp.exec_env_reserved(name)} == set()


def test_exec_env_strips_interpreter_and_loader_controls():
    """禁令 19：命中的名稱移除（不是拒絕整份 spec），其他照給，含名稱有 TOKEN／KEY 的值."""
    kept = {"FIGMA_API_KEY": "f", "MCP_DOCGEN_INPUT_DIR": "/d", "PATH": "/usr/bin", **ENTRY_ENV}
    assert dict(_parse(_document(exec_env={**STRIPPED_ENV, **kept})).exec_env) == kept
    assert [name for name in STRIPPED_ENV if not sp.exec_env_stripped(name)] == []
    assert [name for name in kept if sp.exec_env_stripped(name)] == []


def _evolution_filter_lists() -> tuple[list[str], list[str]]:
    """``strip_sensitive_env`` 裡的黑名單模式與顯式移除清單（函式內的常值，從原始碼讀）."""
    source = textwrap.dedent(inspect.getsource(EvolutionPathGuard.strip_sensitive_env))
    patterns: list[str] = []
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and [getattr(t, "id", "") for t in node.targets] == [
            "_BLACKLIST_PATTERNS"
        ]:
            patterns = ast.literal_eval(node.value)
        if isinstance(node, ast.For) and getattr(node.target, "id", "") == "dangerous":
            names = list(ast.literal_eval(node.iter))
    return patterns, names


def test_stripped_names_follow_the_evolution_sandbox_filter():
    """kernel 不能匯入 evolution_security：兩份清單在這裡對齊（密鑰那一組刻意排除）."""
    patterns, names = _evolution_filter_lists()
    assert patterns and names
    assert set(patterns) - CREDENTIAL_PATTERNS == set(sp.EXEC_ENV_STRIP_PATTERNS)
    assert set(names) == sp.EXEC_ENV_STRIP_NAMES
    sample = {f"X{part}Y": "v" for part in sp.EXEC_ENV_STRIP_PATTERNS} | dict.fromkeys(names, "v")
    assert EvolutionPathGuard.strip_sensitive_env(sample) == {}  # 演化沙箱也會剝掉的
    assert [name for name in sample if not sp.exec_env_stripped(name)] == []


def test_hub_entries_keep_every_env_name_they_need():
    """Hub 範本條目的 env 與 env_requirements 沒有一個被拒收或移除（Notion 等的 token 照給）."""
    hub = json.loads(HUB_EXAMPLE.read_text(encoding="utf-8"))["hub"]
    names = {n for e in hub for n in (*e.get("env", {}), *e.get("env_requirements", []))}
    assert {"NOTION_TOKEN", "JIRA_API_TOKEN", "FIGMA_API_KEY", "SENTRY_ACCESS_TOKEN"} <= names
    assert [n for n in names if sp.exec_env_reserved(n) or sp.exec_env_stripped(n)] == []


def test_exec_env_size_and_count_limits():
    assert (sp.MAX_EXEC_ENV_ITEMS, sp.MAX_EXEC_ENV_BYTES) == (256, 64 * 1024)
    many = {f"V{i}": "" for i in range(sp.MAX_EXEC_ENV_ITEMS)}
    assert len(_parse(_document(exec_env=many)).exec_env) == 256
    assert _code(_document(exec_env={**many, "ONE_MORE": ""})) == "spec_invalid"
    fits = "x" * (sp.MAX_EXEC_ENV_BYTES - len("V") - 2)  # 名稱＋值＋「=」與 NUL
    assert _parse(_document(exec_env={"V": fits})).exec_env == (("V", fits),)
    assert _code(_document(exec_env={"V": fits + "x"})) == "spec_invalid"
    two_bytes = "é" * (len(fits) // 2 + 1)  # 字元數沒超過，位元組超過
    assert _code(_document(exec_env={"V": two_bytes})) == "spec_invalid"


# ── 啟動器：限制之後才交給目標，自己的環境不變 ─────────────────────────────────────────────

LAUNCHER_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/st/home", "U3A_SHARED": "launcher"}


class _FakeOs:
    """``launcher.os`` 的替身：環境是假的（測試不碰真環境）；exec 只記錄，不會取代測試行程."""

    def __init__(self, environ: dict[str, str], record: Callable[[str], None]) -> None:
        self.environ = environ
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []
        self._record = record

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)

    def execvpe(self, file: str, args: list[str], env: dict[str, str]) -> None:
        self._record("exec")
        self.calls.append((file, list(args), dict(env)))

    def execvp(self, _file: str, _args: list[str]) -> None:
        self._record("execvp_without_env")  # 不帶環境的 exec 一律算失敗步驟


def _handoff(monkeypatch, document: dict) -> tuple[list[str], _FakeOs, list[dict[str, str]]]:
    """用假的 Landlock 跑 ``launch``、真的 ``_exec``；記下每一步當下啟動器自己的環境."""
    steps: list[str] = []
    seen: list[dict[str, str]] = []

    def _record(name: str) -> None:
        steps.append(name)
        seen.append(dict(fake.environ))

    def _step(name: str, result: object = None) -> Callable[..., object]:
        return lambda *_args: _record(name) or result

    fake = _FakeOs(dict(LAUNCHER_ENV), _record)
    raw = json.dumps(document).encode("utf-8")
    monkeypatch.setattr(launcher, "os", fake)
    monkeypatch.setattr(launcher, "_read_spec", lambda _fd: raw)
    monkeypatch.setattr(launcher, "_linux_landlock", lambda: (object(), 7))
    monkeypatch.setattr(launcher, "_set_process_attributes", _step("attrs"))
    monkeypatch.setattr(launcher, "apply_sandbox", _step("sandbox", []))
    monkeypatch.setattr(launcher, "_apply_rlimits", _step("rlimits"))
    monkeypatch.setattr(launcher, "_close_inherited_fds", _step("close_fds"))
    launcher.launch(launcher.Command("launch", spec_fd=3, argv=("node", "server.js")))
    return steps, fake, seen


def test_launcher_hands_exec_env_to_execvpe_only_after_the_restrictions(monkeypatch):
    steps, fake, seen = _handoff(monkeypatch, _document(exec_env={**ENTRY_ENV, **STRIPPED_ENV}))
    assert steps == ["attrs", "sandbox", "rlimits", "close_fds", "exec"]
    assert fake.calls == [("node", ["node", "server.js"], {**LAUNCHER_ENV, **ENTRY_ENV})]
    assert fake.calls[0][2]["U3A_SHARED"] == "entry"  # 同名時條目的值蓋過啟動器的
    assert not set(STRIPPED_ENV) & set(fake.calls[0][2])  # 禁令 19 那一套目標拿不到
    assert seen == [LAUNCHER_ENV] * len(steps)  # 限制之前、之後都沒套進自己的環境
    assert fake.environ == LAUNCHER_ENV


def test_launcher_without_exec_env_passes_its_own_environment_unchanged(monkeypatch):
    _, fake, _ = _handoff(monkeypatch, _document())
    assert fake.calls == [("node", ["node", "server.js"], LAUNCHER_ENV)]


# ── 舊啟動器與執行器 ────────────────────────────────────────────────────────────────


def test_launcher_that_predates_exec_env_refuses_it_and_runs_nothing(monkeypatch):
    """``/opt`` 的舊啟動器只認舊欄位：看到 ``exec_env`` 以 ``spec_invalid`` 拒絕（fail-closed）.

    spec 版本不變，所以沒帶 ``exec_env`` 的 spec 新舊兩邊都照收。
    """
    assert sp.SPEC_VERSION == 1
    reached: list[str] = []
    monkeypatch.setattr(sp, "_TOP_KEYS", PRE_C2_3_TOP_KEYS)
    monkeypatch.setattr(launcher, "_linux_landlock", lambda: reached.append("landlock"))
    monkeypatch.setattr(launcher, "_exec", lambda *_args: reached.append("exec") or 0)
    assert _parse(_document()).exec_env == ()
    spec_r, spec_w = os.pipe()
    status_r, status_w = os.pipe()
    os.write(spec_w, json.dumps(_document(exec_env={"TOKEN": "t"})).encode())
    os.close(spec_w)
    os.set_blocking(status_r, False)
    try:
        argv = ["--spec-fd", str(spec_r), "--status-fd", str(status_w), "--", "node", "server.js"]
        assert launcher.main(argv) == sp.FAILURE_EXIT
        assert os.read(status_r, 4096) == b"spec_invalid\n"
    finally:
        os.close(status_r)
    assert reached == []


PD, STATE = "/srv/data/personal_drive", "/srv/executor_state"
LAYOUT = es.Layout(rw=(PD,), ro=(), state=STATE, exec_paths=es.SYSTEM_EXEC_PATHS)


def _start(exec_env: dict[str, str]) -> dict:
    home, tmp = LAYOUT.state_dirs("agents", "Alice")
    target = f"{PD}/Alice"
    spec_document = _document(
        rules=[{"path": p, "access": "rwx"} for p in (target, home, tmp)],
        block_ports=sorted(sp.DEFAULT_BLOCK_PORTS), exec_env=exec_env,
    )
    return {
        "argv": ["node", "server.js"], "cwd": target, "env": {"PATH": "/usr/bin:/bin"},
        "spec": spec_document, "kind": "agents", "owner": "Alice", "io": "merged",
        "max_seconds": 60,
    }


def _check(document: dict) -> es.StartPlan:
    return es.check_start(
        document, spec=sp, layout=LAYOUT, hello_ports=frozenset(sp.DEFAULT_BLOCK_PORTS),
        server_pid=4321,
    )


def test_executor_start_carries_exec_env_in_the_spec_not_in_the_launcher_env():
    plan = _check(_start(ENTRY_ENV))
    assert plan.env == {"PATH": "/usr/bin:/bin"}
    assert json.loads(plan.spec)["exec_env"] == ENTRY_ENV
    with pytest.raises(es.Rejected) as caught:
        _check(_start({"HOME": "/srv/data/personal_drive/Bob"}))
    assert caught.value.reason == "spec_invalid"


def test_executor_start_env_allows_uv_python_downloads_and_nothing_from_exec_env():
    env = {"PATH": "/usr/bin", "UV_PYTHON_DOWNLOADS": "never"}
    assert "UV_PYTHON_DOWNLOADS" in sp.EXECUTOR_ENV_ALLOWED
    assert sp.executor_env_issue(env) is None
    assert sp.filter_executor_env({**env, **ENTRY_ENV}) == env
    assert sp.executor_env_issue({**env, "LD_PRELOAD": "/srv/x.so"}) == "env_name"
