"""Z9 測試期間絆線的自我測試（ADR-0293 Z9）.

產品程式的呼叫者用 ``tmp_path`` 裡臨時匯入的模組模擬（不是標準函式庫、site-packages，也不在
``tests/`` 底下）；事件用 ``sys.audit`` 發，不真的開檔或啟動子行程。每個測試把自己造成的絆線
紀錄用 ``take_records_for_self_test`` 取走（Z8 斷言只有這個檔案能呼叫它）。
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from memory_server.utils.zone_paths import Anchor, HandoffKind, ZonePath
from src.services.resource_manager import resource_manager as rm

from tests.support import zone_tripwire
from tests.support.links import make_dir_link

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
PROBE = """
import sys


def emit(event, *args):
    sys.audit(event, *args)


def read(path):
    with open(path, "rb") as handle:
        return handle.read()
"""


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """一個「產品程式」：``tmp_path`` 裡的模組，絆線把它當成產品的呼叫者."""
    folder = tmp_path / "probe_pkg"
    folder.mkdir()
    name = f"z9_probe_{os.getpid()}_{id(tmp_path)}"
    (folder / f"{name}.py").write_text(PROBE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(folder))
    module = importlib.import_module(name)
    yield module
    sys.modules.pop(name, None)


@pytest.fixture
def drive(zone_data_root: Path) -> Path:
    folder = zone_data_root / "personal_drive" / "Alice"
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("notes", encoding="utf-8")
    return folder


def _taken() -> list[tuple[str, str | None, str]]:
    return [(r.error, r.display, r.detail) for r in zone_tripwire.take_records_for_self_test()]


def test_a_product_read_inside_a_drive_is_recorded(probe, drive):
    assert probe.read(drive / "notes.txt") == b"notes"
    [(event, display, detail)] = _taken()
    assert event == "open"
    assert display == str(drive / "notes.txt")
    assert detail == f"{probe.__name__}:read"


def test_a_read_beside_the_drive_is_not_recorded(probe, zone_data_root):
    config = zone_data_root / "custom" / "M1" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    assert probe.read(config) == b"{}"
    probe.emit("os.mkdir", str(zone_data_root / "personal_drive"), 0o777, None)
    assert _taken() == []


def test_tests_and_the_primitives_are_allowed(drive):
    assert (drive / "notes.txt").read_text("utf-8") == "notes"
    assert rm.zone_read_text(ZonePath(Anchor.PERSONAL_DRIVE, ("Alice", "notes.txt"))) == "notes"
    assert _taken() == []


def test_a_recursive_event_on_a_folder_that_holds_a_drive_is_recorded(probe, zone_data_root, drive):
    probe.emit("shutil.rmtree", str(zone_data_root), None)
    probe.emit("os.listdir", str(zone_data_root))
    assert [event for event, _display, _detail in _taken()] == ["shutil.rmtree"]


def test_a_handoff_is_allowed_only_for_its_kind_and_only_while_held(probe, drive):
    zp = ZonePath(Anchor.PERSONAL_DRIVE, ("Alice",))
    with rm.zone_handoff(zp, HandoffKind.SUBPROCESS_CWD) as cwd:
        probe.emit("subprocess.Popen", sys.executable, ["x"], cwd, None)
        probe.emit("_winapi.CreateProcess", sys.executable, "x", cwd)
        assert _taken() == []
        probe.emit("open", cwd, "r", 0)
        assert [event for event, _display, _detail in _taken()] == ["open"]
    probe.emit("subprocess.Popen", sys.executable, ["x"], cwd, None)
    assert [event for event, _display, _detail in _taken()] == ["subprocess.Popen"]


def test_a_subprocess_argument_is_data_not_a_path(probe, drive):
    probe.emit("subprocess.Popen", sys.executable, [str(drive / "notes.txt")], None, None)
    probe.emit("_winapi.CreateProcess", sys.executable, str(drive / "notes.txt"), None)
    assert _taken() == []


def test_a_skill_venv_python_as_the_program_is_data(probe, zone_data_root, drive):
    """執行檔在 MCP_SERVERS 裡（技能 venv 的 python，P4）是資料；別的錨點與 cwd 照舊記."""
    python = zone_data_root / "mcp_servers" / "managed" / "skill" / ".venv" / "bin" / "python"
    probe.emit("subprocess.Popen", str(python), ["x"], None, None)
    probe.emit("os.posix_spawn", str(python), ["x"], {})
    probe.emit("_winapi.CreateProcess", str(python), "x", None)
    assert _taken() == []
    probe.emit("subprocess.Popen", str(drive / "notes.txt"), ["x"], None, None)
    probe.emit("subprocess.Popen", sys.executable, ["x"], str(python.parent), None)
    assert [event for event, _display, _detail in _taken()] == ["subprocess.Popen"] * 2


def test_the_mode_of_open_is_not_a_path(probe, zone_data_root, drive, monkeypatch):
    """工作目錄在區裡時，``open`` 的 mode 字串不會被當成相對路徑；路徑本身照舊看."""
    config = zone_data_root / "custom" / "M1" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(drive)
    assert probe.read(config) == b"{}"
    probe.emit("open", str(config), "r", 0)
    assert _taken() == []
    probe.emit("open", "notes.txt", "r", 0)
    assert [(event, display) for event, display, _detail in _taken()] == [("open", "notes.txt")]


def test_a_network_address_is_not_a_path(probe, drive, monkeypatch):
    """工作目錄在區裡時，socket 的 (host, port) 與名稱查詢不算路徑；AF_UNIX 的字串位址照舊看."""
    monkeypatch.chdir(drive)
    probe.emit("socket.bind", object(), ("127.0.0.1", 0))
    probe.emit("socket.connect", object(), ("localhost", 8000))
    probe.emit("socket.sendto", object(), ("::1", 8000, 0, 0))
    probe.emit("socket.getaddrinfo", "localhost", 8000, 0, 0, 0)
    probe.emit("socket.gethostbyname", "localhost")
    assert _taken() == []
    probe.emit("socket.bind", object(), "notes.sock")
    assert [(event, display) for event, display, _detail in _taken()] == [
        ("socket.bind", "notes.sock")
    ]


def test_a_directory_fd_is_followed_on_linux(probe, drive, tmp_path):
    if sys.platform != "linux":  # 沒有 /proc/self/fd：dir_fd 還原不了，也不猜
        assert zone_tripwire._fd_path(0) is None
        return
    fd = os.open(drive, os.O_RDONLY)
    try:
        probe.emit("os.mkdir", "new", 0o777, fd)
        probe.emit("os.listdir", fd)
        assert [display for _event, display, _detail in _taken()] == [
            str(drive / "new"),
            str(drive),
        ]
        # 同一個號碼出現在 mode 的位置：不是 dir_fd，不當成路徑
        probe.emit("os.mkdir", str(tmp_path / "plain"), fd, None)
        assert _taken() == []
    finally:
        os.close(fd)


def test_an_unregistered_proc_fd_path_inside_a_drive_is_recorded(probe, drive):
    """Linux：只有回檔登記過的 ``/proc/self/fd`` 字串放行；沒登記的照樣記."""
    if sys.platform != "linux":  # 沒有 /proc/self/fd
        return
    fd = os.open(drive / "notes.txt", os.O_RDONLY)
    try:
        assert probe.read(f"/proc/self/fd/{fd}") == b"notes"
    finally:
        os.close(fd)
    assert [(event, display) for event, display, _detail in _taken()] == [
        ("open", f"/proc/self/fd/{fd}")
    ]


def test_a_zone_path_argument_is_left_alone(probe, drive):
    """``ZonePath`` 轉字串會通知觀察者；絆線不碰它，不留下任何紀錄."""
    probe.emit("open", ZonePath(Anchor.PERSONAL_DRIVE, ("Alice", "notes.txt")), "r", 0)
    assert _taken() == []


def test_an_error_in_the_hook_is_recorded(probe, drive, monkeypatch):
    def broken() -> None:
        raise RuntimeError("broken")

    # 只在發事件的這段換掉：teardown 時產品程式的其他事件不該碰到壞掉的根來源
    with monkeypatch.context() as patch:
        patch.setattr(zone_tripwire, "current_roots", broken)
        probe.emit("open", str(drive / "notes.txt"), "r", 0)
    [(event, _display, detail)] = _taken()
    assert event == "hook_error"
    assert "broken" in detail


def test_allowed_entries_that_were_never_hit_are_listed(monkeypatch):
    monkeypatch.setitem(zone_tripwire.ALLOWED, "nowhere.module:never", "測試")
    assert "nowhere.module:never" in zone_tripwire.missing_hits()


def test_a_linux_only_allowance_is_required_only_on_linux(monkeypatch):
    """權限自癒只在 Linux 經 ``/proc/self/fd`` 改權限：別的平台全量檢查不要求它命中."""
    monkeypatch.setattr(zone_tripwire, "_hits", set())
    [key] = zone_tripwire.LINUX_ONLY_ALLOWED
    monkeypatch.setattr(zone_tripwire, "_ON_LINUX", True)
    assert key in zone_tripwire.missing_hits()
    monkeypatch.setattr(zone_tripwire, "_ON_LINUX", False)
    assert key not in zone_tripwire.missing_hits()
    assert "memory_server.utils.no_follow:*" in zone_tripwire.missing_hits()


def test_every_allowance_names_code_that_exists():
    """放行的「模組:函式」改名或搬走時這裡先紅，不會留下對不到程式的放行."""
    assert zone_tripwire.LINUX_ONLY_ALLOWED.issubset(zone_tripwire.ALLOWED)
    for key in zone_tripwire.ALLOWED:
        module_name, _, qualname = key.partition(":")
        target: object = importlib.import_module(module_name)
        if qualname != "*":
            for part in qualname.split("."):
                target = getattr(target, part)
        assert target is not None, key


def test_code_that_runs_in_another_process_in_production_is_left_alone(probe, drive, monkeypatch):
    """堆疊裡有明列的「正式環境在別的行程跑」的模組（執行器伺服器）就不記；其他照記."""
    monkeypatch.setitem(zone_tripwire.OTHER_PROCESS_MODULES, probe.__name__, "測試")
    assert probe.read(drive / "notes.txt") == b"notes"
    assert _taken() == []
    monkeypatch.delitem(zone_tripwire.OTHER_PROCESS_MODULES, probe.__name__)
    assert probe.read(drive / "notes.txt") == b"notes"
    assert [event for event, _display, _detail in _taken()] == ["open"]


def test_the_proxy_only_takes_the_digest_from_other_process_modules():
    """proxy 只從執行器伺服器拿程式雜湊，不在自己的行程裡跑它：絆線不看它，不會漏掉 proxy 的 I/O."""
    others = zone_tripwire.OTHER_PROCESS_MODULES
    used: dict[str, set[str]] = {}
    for path in (ROOT / "ToukaProxy" / "src").rglob("*.py"):
        module = ".".join(path.relative_to(ROOT / "ToukaProxy").with_suffix("").parts)
        if module in others:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names if a.name in others]
                used.setdefault(module, set()).update(f"import {name}" for name in names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in others:
                    used.setdefault(module, set()).update(a.name for a in node.names)
                parent = node.module + "."
                for alias in node.names:
                    if parent + alias.name in others:
                        used.setdefault(module, set()).add(f"module {alias.name}")
    assert {module: names for module, names in used.items() if names} == {
        "src.services.isolation.status": {"executor_code_digest"}
    }


def test_the_full_run_flag_is_not_inherited_by_child_runs():
    """全量檢查只給最外層的 session：測試再開的 pytest 只跑一部分，環境裡不能還有這個旗標."""
    assert zone_tripwire.FULL_RUN_ENV not in os.environ  # noqa: ENV_SSOT_VIOLATION - 驗 conftest 已移除


def test_an_allowed_caller_counts_as_hit_only_inside_a_zone(probe, drive, tmp_path, monkeypatch):
    key = f"{probe.__name__}:read"
    monkeypatch.setitem(zone_tripwire.ALLOWED, key, "測試")
    monkeypatch.setattr(zone_tripwire, "_hits", set())
    (tmp_path / "plain.txt").write_text("p", encoding="utf-8")
    probe.read(tmp_path / "plain.txt")
    assert key in zone_tripwire.missing_hits()
    probe.read(drive / "notes.txt")
    assert key not in zone_tripwire.missing_hits()
    assert _taken() == []


def test_removing_a_link_that_points_into_a_drive_is_not_recorded(probe, zone_data_root, drive):
    """刪除與改名動的是連結本身；經連結讀到裡面的檔案照樣記."""
    link = zone_data_root / "proj" / "bob"
    link.parent.mkdir()
    make_dir_link(link, drive)
    probe.emit("os.remove", str(link), None)
    probe.emit("os.rename", str(link), str(zone_data_root / "proj" / "carol"), None, None)
    assert _taken() == []
    probe.read(link / "notes.txt")
    assert [event for event, _display, _detail in _taken()] == ["open"]


def test_removing_a_link_inside_a_drive_is_recorded(probe, zone_data_root, drive):
    outside = zone_data_root / "elsewhere"
    outside.mkdir()
    make_dir_link(drive / "out", outside)
    probe.emit("os.remove", str(drive / "out"), None)
    assert [display for _event, display, _detail in _taken()] == [str(drive / "out")]


def test_a_named_pipe_is_left_alone(probe):
    """絆線不碰裝置命名空間：碰一次就佔掉管道唯一的連線，子程序的管道會建不起來（WinError 231）."""
    winapi = pytest.importorskip("_winapi", reason="具名管道只在 Windows")
    name = rf"\\.\pipe\z9-probe-{os.getpid()}"
    server = winapi.CreateNamedPipe(
        name, winapi.PIPE_ACCESS_INBOUND, winapi.PIPE_WAIT, 1, 4096, 4096, 0, winapi.NULL
    )
    try:
        probe.emit("open", name, "r", 0)
        client = winapi.CreateFile(
            name, winapi.GENERIC_WRITE, 0, winapi.NULL, winapi.OPEN_EXISTING, 0, winapi.NULL
        )
        winapi.CloseHandle(client)
    finally:
        winapi.CloseHandle(server)
    assert _taken() == []
