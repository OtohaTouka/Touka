"""Windows 呼叫的純函式、設定檔與安裝規則（ADR-0312）；真 Windows 的部分用 skipif。"""

from __future__ import annotations

import runpy
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

if sys.platform == "win32":
    import _winapi

import install as installer
import pytest
from touka_local import settings as settings_module
from touka_local.errors import Refused
from touka_local.settings import Settings, load_environment, load_settings, save_settings
from touka_local.winapi import (
    DELETE_FLAGS,
    build_from_buffer,
    drive_kind_name,
    interpret_delete,
    structure_size,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_LOCAL = Path(__file__).resolve().parents[3] / "ToukaLocal"

is_windows = sys.platform == "win32"


def test_from_buffer_is_double_null_terminated() -> None:
    assert build_from_buffer("D:\\Open\\a.txt") == "D:\\Open\\a.txt\0\0"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative\\a.txt",
        "\\\\?\\D:\\a.txt",
        "\\\\server\\share\\a",
        "D:\\a\0b",
        "D:a.txt",
        "\\rooted",
    ],
)
def test_from_buffer_refuses_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        build_from_buffer(path)


def test_delete_flags() -> None:
    allow_undo = 0x0040
    no_confirmation = 0x0010
    no_error_ui = 0x0400
    want_nuke_warning = 0x4000
    silent = 0x0004
    no_connected_elements = 0x2000  # 不然丟 a.html 會連 a_files 一起丟，清單上卻沒列
    assert (
        allow_undo
        | no_confirmation
        | no_error_ui
        | want_nuke_warning
        | silent
        | no_connected_elements
        == DELETE_FLAGS
    )
    assert DELETE_FLAGS <= 0xFFFF


@pytest.mark.parametrize(
    ("code", "aborted", "outcome"),
    [
        (0, 0, "ok"),
        (0, 1, "aborted"),
        (2, 0, "failed"),
        (2, 1, "failed"),
    ],
)
def test_interpret_delete(code: int, aborted: int, outcome: str) -> None:
    assert interpret_delete(code, aborted) == outcome


@pytest.mark.parametrize(
    ("code", "name"),
    [
        (0, "unknown"),
        (1, "unknown"),
        (2, "removable"),
        (3, "fixed"),
        (4, "remote"),
        (5, "cdrom"),
        (6, "ramdisk"),
        (99, "unknown"),
    ],
)
def test_drive_kind(code: int, name: str) -> None:
    assert drive_kind_name(code) == name


@pytest.mark.skipif(not is_windows, reason="只有 Windows 有這個結構的實際大小")
def test_structure_size_on_windows() -> None:
    assert structure_size() == 56


# --- 設定 ---


def test_settings_round_trip(tmp_path: Path) -> None:
    file = tmp_path / "settings.json"
    save_settings(file, Settings(port=8123, token="t" * 43, touka_folder="D:\\Touka"))
    assert load_settings(file) == Settings(port=8123, token="t" * 43, touka_folder="D:\\Touka")


def test_missing_settings_use_defaults(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "none.json") == Settings(
        port=8000, token=None, touka_folder=None
    )


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"port": "8000"}',
        '{"port": 0}',
        '{"port": 70000}',
        '{"port": true}',
        '{"token": 1}',
        '{"touka_folder": 1}',
        '{"unknown": 1}',
    ],
)
def test_broken_settings_fall_back_to_nothing(tmp_path: Path, content: str) -> None:
    file = tmp_path / "s.json"
    file.write_text(content, encoding="utf-8")
    assert load_settings(file) == Settings(port=8000, token=None, touka_folder=None)


_ENV = {
    "USERPROFILE": "C:\\Users\\Me",
    "WINDIR": "C:\\Windows",
    "ProgramFiles": "C:\\Program Files",
    "ProgramFiles(x86)": "C:\\Program Files (x86)",
    "ProgramData": "C:\\ProgramData",
    "LOCALAPPDATA": "C:\\Users\\Me\\AppData\\Local",
    "APPDATA": "C:\\Users\\Me\\AppData\\Roaming",
}


def test_environment_from_system_values() -> None:
    env = load_environment(_ENV, "D:\\Touka")
    assert env.user_profile == "C:\\Users\\Me"
    assert env.windows_dir == "C:\\Windows"
    assert env.program_files == ("C:\\Program Files", "C:\\Program Files (x86)")
    assert env.program_data == "C:\\ProgramData"
    assert env.app_data == "C:\\Users\\Me\\AppData\\Roaming"
    assert env.local_app_data == "C:\\Users\\Me\\AppData\\Local"
    assert env.helper_dir == "C:\\Users\\Me\\AppData\\Local\\ToukaLocal"
    assert env.touka_folder == "D:\\Touka"


@pytest.mark.parametrize(
    "missing", ["USERPROFILE", "WINDIR", "ProgramFiles", "ProgramData", "LOCALAPPDATA", "APPDATA"]
)
def test_environment_refuses_when_a_value_is_missing(missing: str) -> None:
    values = {k: v for k, v in _ENV.items() if k != missing}
    with pytest.raises(Refused) as caught:
        load_environment(values, "D:\\Touka")
    assert caught.value.code == "bad_environment"


@pytest.mark.parametrize("touka", [None, "", "relative"])
def test_environment_needs_the_touka_folder(touka: str | None) -> None:
    with pytest.raises(Refused) as caught:
        load_environment(_ENV, touka)
    assert caught.value.code == "bad_environment"


# --- 安裝 ---


SOURCE = "C:\\Users\\Me\\AppData\\Local\\Temp\\touka-clone\\ToukaLocal"
PYTHON = "C:\\Users\\Me\\AppData\\Roaming\\uv\\python\\cpython-3.12\\python.exe"
APPDATA = "C:\\Users\\Me\\AppData\\Local"
TOUKA = "D:\\Touka"


def test_install_plan_paths() -> None:
    plan = installer.plan_install(
        source=SOURCE, local_app_data=APPDATA, python=PYTHON, touka_folder=TOUKA
    )
    assert plan.app_dir == f"{APPDATA}\\ToukaLocal\\app"
    assert plan.pythonw == PYTHON.replace("python.exe", "pythonw.exe")
    assert plan.launcher == f"{APPDATA}\\ToukaLocal\\ToukaLocal.cmd"


def test_launcher_uses_absolute_paths_and_isolated_python() -> None:
    plan = installer.plan_install(
        source=SOURCE, local_app_data=APPDATA, python=PYTHON, touka_folder=TOUKA
    )
    text = installer.launcher_text(plan)
    assert f'"{plan.pythonw}" -I -S "{plan.app_dir}"' in text
    assert text.startswith("@echo off")


@pytest.mark.parametrize(
    ("source", "python"),
    [
        (f"{TOUKA}\\ToukaLocal", PYTHON),
        (f"{TOUKA.lower()}\\ToukaLocal", PYTHON),
        (SOURCE, f"{TOUKA}\\.venv\\Scripts\\python.exe"),
    ],
)
def test_install_refuses_anything_from_the_touka_folder(source: str, python: str) -> None:
    with pytest.raises(installer.InstallRefused):
        installer.plan_install(
            source=source, local_app_data=APPDATA, python=python, touka_folder=TOUKA
        )


@pytest.mark.parametrize("touka", ["relative", "", "\\\\server\\share"])
def test_install_needs_absolute_touka_folder(touka: str) -> None:
    with pytest.raises(installer.InstallRefused):
        installer.plan_install(
            source=SOURCE, local_app_data=APPDATA, python=PYTHON, touka_folder=touka
        )


@pytest.mark.parametrize("appdata", ['C:\\Us"er', "C:\\100%\\x", "C:\\a&b", "C:\\a^b"])
def test_install_refuses_paths_a_launcher_cannot_quote(appdata: str) -> None:
    with pytest.raises(installer.InstallRefused):
        installer.plan_install(
            source=SOURCE, local_app_data=appdata, python=PYTHON, touka_folder=TOUKA
        )


def test_allowed_files_are_exactly_the_helper_sources() -> None:
    found = {
        path.relative_to(_LOCAL).as_posix()
        for path in _LOCAL.rglob("*.py")
        if path.name != "install.py" and "__pycache__" not in path.parts
    }
    assert set(installer.ALLOWED_FILES) == found


def test_install_copies_only_allowed_files(tmp_path: Path) -> None:
    source = tmp_path / "src"
    for relative in installer.ALLOWED_FILES:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {relative}\n", encoding="utf-8")
    (source / "evil.py").write_text("x", encoding="utf-8")
    app = tmp_path / "app"
    (app / "touka_local").mkdir(parents=True)
    (app / "stale.py").write_text("old", encoding="utf-8")
    installer.copy_allowed(source, app)
    copied = {p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file()}
    assert copied == set(installer.ALLOWED_FILES)


def test_install_refuses_when_a_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(installer.InstallRefused):
        installer.copy_allowed(tmp_path / "empty", tmp_path / "app")


@pytest.mark.parametrize(("isolated", "no_site"), [(0, 1), (1, 0), (0, 0)])
def test_install_needs_isolated_python(isolated: int, no_site: int) -> None:
    with pytest.raises(installer.InstallRefused):
        installer.check_flags(isolated=isolated, no_site=no_site)


def test_install_accepts_isolated_python() -> None:
    installer.check_flags(isolated=1, no_site=1)


def test_update_settings_keeps_pairing(tmp_path: Path) -> None:
    file = tmp_path / "settings.json"
    save_settings(file, Settings(port=8123, token="t" * 43, touka_folder="D:\\Old"))
    installer.update_settings(file, "D:\\Touka")
    assert load_settings(file) == Settings(port=8123, token="t" * 43, touka_folder="D:\\Touka")


def test_update_settings_from_nothing(tmp_path: Path) -> None:
    file = tmp_path / "nested" / "settings.json"
    installer.update_settings(file, "D:\\Touka")
    assert load_settings(file) == Settings(port=8000, token=None, touka_folder="D:\\Touka")


def test_launcher_bytes_follow_the_requested_code_page() -> None:
    plan = installer.plan_install(
        source=SOURCE, local_app_data=APPDATA, python=PYTHON, touka_folder=TOUKA
    )
    assert installer.launcher_bytes(plan, "ascii").decode("ascii") == installer.launcher_text(plan)


def test_launcher_refuses_a_path_the_code_page_cannot_hold() -> None:
    plan = installer.plan_install(
        source=SOURCE,
        local_app_data="C:\\Users\\千夏\\AppData\\Local",
        python=PYTHON,
        touka_folder=TOUKA,
    )
    with pytest.raises(installer.InstallRefused):
        installer.launcher_bytes(plan, "ascii")


def test_touka_folder_must_exist(tmp_path: Path) -> None:
    with pytest.raises(installer.InstallRefused):
        installer.check_touka_folder(tmp_path / "none")
    installer.check_touka_folder(tmp_path)


def test_install_stops_when_the_touka_folder_does_not_exist(monkeypatch) -> None:
    monkeypatch.setattr(installer, "check_flags", lambda **_flags: None)
    environ = {"LOCALAPPDATA": "C:\\Users\\Me\\AppData\\Local"}
    with pytest.raises(installer.InstallRefused):
        installer._plan("Z:\\NoSuchTouka", environ)


def test_finish_message_mentions_a_missing_desktop() -> None:
    assert "桌面" not in installer.finish_message("L.cmd", on_desktop=True)
    message = installer.finish_message("L.cmd", on_desktop=False)
    assert "桌面" in message
    assert "L.cmd" in message


def test_helper_checks_isolation_before_loading_anything() -> None:
    source = (Path(installer.__file__).parent / "__main__.py").read_text(encoding="utf-8")
    assert source.index("sys.flags.isolated") < source.index("from touka_local")


def test_update_settings_keeps_pairing_left_in_a_temporary_file(tmp_path: Path) -> None:
    file = tmp_path / "settings.json"
    save_settings(file, Settings(port=8123, token="t" * 43, touka_folder="D:\\Old"))
    file.rename(file.with_name("settings.json.new"))
    installer.update_settings(file, "D:\\Touka")
    assert load_settings(file) == Settings(port=8123, token="t" * 43, touka_folder="D:\\Touka")


def _run_entry(monkeypatch, *, isolated: int) -> tuple[object, list[bool]]:
    called: list[bool] = []
    fake = types.ModuleType("touka_local.app")

    def main() -> int:
        called.append(True)
        return 7

    fake.main = main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "touka_local.app", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    # 只換 isolated：runpy 第一次 import 東西時還要讀其他旗標
    flags = {name: getattr(sys.flags, name) for name in sys.flags.__match_args__}
    monkeypatch.setattr(sys, "flags", types.SimpleNamespace(**{**flags, "isolated": isolated}))
    entry = Path(installer.__file__).parent / "__main__.py"
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(entry))
    return caught.value.code, called


def test_helper_starts_in_isolated_mode(monkeypatch) -> None:
    assert _run_entry(monkeypatch, isolated=1) == (7, [True])


def test_helper_refuses_to_start_without_isolated_mode(monkeypatch) -> None:
    code, called = _run_entry(monkeypatch, isolated=0)
    assert isinstance(code, str)
    assert called == []


def test_environment_knows_where_python_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module.sys, "executable", "E:\\Py\\pythonw.exe")
    environ = {
        "USERPROFILE": "C:\\Users\\Me",
        "WINDIR": "C:\\Windows",
        "ProgramFiles": "C:\\Program Files",
        "ProgramData": "C:\\ProgramData",
        "APPDATA": "C:\\A",
        "LOCALAPPDATA": "C:\\L",
    }
    monkeypatch.setattr(settings_module.sys, "base_prefix", "E:\\Base")
    assert load_environment(environ, "D:\\Touka").code_paths == (
        "E:\\Py",
        "E:\\Base",
        f"C:\\Users\\Me\\Desktop\\{installer._LAUNCHER_NAME}",
        f"C:\\A\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{installer._LAUNCHER_NAME}",
    )


# --- 照 Touka 的模式核准的三個小檔（ADR-0317） ---


def test_install_never_turns_the_switch_on() -> None:
    """鑰匙放在 Touka 的資料夾，照模式核准要使用者在小幫手視窗自己打開（ADR-0317）。"""
    source = Path(installer.__file__).read_text(encoding="utf-8")
    assert settings_module.MODE_FOLLOWING_FILE not in source
    assert not hasattr(installer, "ensure_mode_following")


def test_reinstall_leaves_the_key_switch_and_reset_alone(tmp_path: Path) -> None:
    helper = tmp_path / "ToukaLocal"
    key_file = helper / settings_module.DECISION_KEY_FILE
    switch_file = helper / settings_module.MODE_FOLLOWING_FILE
    reset_file = helper / settings_module.MODE_RESET_FILE
    settings_module.save_decision_key(key_file, bytes(range(32)))
    settings_module.save_mode_following(switch_file, enabled=False)
    settings_module.save_mode_reset(reset_file, 1234.5)
    before = {file: file.read_bytes() for file in (key_file, switch_file, reset_file)}
    source = tmp_path / "src"
    for relative in installer.ALLOWED_FILES:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {relative}\n", encoding="utf-8")
    installer.copy_allowed(source, helper / "app")
    installer.update_settings(helper / "settings.json", "D:\\Touka")
    assert {file: file.read_bytes() for file in before} == before


# --- 一行指令安裝：互動詢問、開機自動啟動、整個換掉 app（ADR-0329） ---

STARTUP = "C:\\Users\\Me\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs\\Startup"
APP_DIR = f"{APPDATA}\\ToukaLocal\\app"


def _answers(*replies: str) -> tuple[list[str], Callable[[str], str]]:
    asked: list[str] = []
    queue = list(replies)

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return queue.pop(0)

    return asked, ask


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  D:\\Touka  ", "D:\\Touka"),
        ('"D:\\My Touka"', "D:\\My Touka"),
        ('  "D:\\Touka"  ', "D:\\Touka"),
        ('"D:\\Touka', '"D:\\Touka'),
        ("", ""),
    ],
)
def test_folder_answer_is_cleaned_like_the_settings_page(raw: str, expected: str) -> None:
    assert installer.clean_folder_input(raw) == expected


def test_given_folder_is_used_without_asking() -> None:
    asked, ask = _answers()
    assert installer.resolve_touka_folder('"D:\\Touka"', "D:\\Old", ask) == "D:\\Touka"
    assert asked == []


def test_empty_answer_keeps_the_current_folder() -> None:
    asked, ask = _answers("")
    assert installer.resolve_touka_folder(None, "D:\\Old", ask) == "D:\\Old"
    assert "D:\\Old" in asked[0]


def test_typed_answer_replaces_the_current_folder() -> None:
    _asked, ask = _answers(' "E:\\Touka" ')
    assert installer.resolve_touka_folder(None, "D:\\Old", ask) == "E:\\Touka"


def test_empty_answer_without_a_current_folder_refuses() -> None:
    _asked, ask = _answers("   ")
    with pytest.raises(installer.InstallRefused):
        installer.resolve_touka_folder(None, None, ask)


def test_non_interactive_uses_the_current_folder() -> None:
    assert installer.resolve_touka_folder(None, "D:\\Old", None) == "D:\\Old"


def test_non_interactive_without_any_folder_refuses() -> None:
    with pytest.raises(installer.InstallRefused):
        installer.resolve_touka_folder(None, None, None)


def test_current_folder_comes_from_settings(tmp_path: Path) -> None:
    file = tmp_path / "settings.json"
    assert installer.current_touka_folder(file) is None
    save_settings(file, Settings(port=8000, token=None, touka_folder="D:\\Old"))
    assert installer.current_touka_folder(file) == "D:\\Old"


def test_startup_launcher_location() -> None:
    assert installer.startup_launcher("C:\\Users\\Me\\AppData\\Roaming") == (
        f"{STARTUP}\\{installer._LAUNCHER_NAME}"
    )


def test_installer_and_helper_agree_on_the_startup_launcher() -> None:
    environ = {
        "USERPROFILE": "C:\\Users\\Me",
        "WINDIR": "C:\\Windows",
        "ProgramFiles": "C:\\Program Files",
        "ProgramData": "C:\\ProgramData",
        "APPDATA": "C:\\Users\\Me\\AppData\\Roaming",
        "LOCALAPPDATA": APPDATA,
    }
    code_paths = load_environment(environ, "D:\\Touka").code_paths
    assert installer.startup_launcher(environ["APPDATA"]) in code_paths


def _our_launcher() -> bytes:
    plan = installer.plan_install(
        source=SOURCE, local_app_data=APPDATA, python=PYTHON, touka_folder=TOUKA
    )
    assert plan.app_dir == APP_DIR
    return installer.launcher_bytes(plan, "ascii")


def test_our_launcher_is_recognised() -> None:
    assert installer.is_our_launcher(_our_launcher(), APP_DIR, "ascii")


def test_an_older_python_in_our_launcher_still_counts() -> None:
    old = _our_launcher().replace(b"cpython-3.12", b"cpython-3.11")
    assert installer.is_our_launcher(old, APP_DIR, "ascii")


_OUR_APP = b'"C:\\Users\\Me\\AppData\\Local\\ToukaLocal\\app"'


@pytest.mark.parametrize(
    "data",
    [
        b'@echo off\r\nstart "" "C:\\Py\\pythonw.exe" -I -S "C:\\Other\\app"\r\n',
        b'@echo off\r\nstart "" "C:\\Py\\pythonw.exe" -I -S ' + _OUR_APP + b"\r\ndel x\r\n",
        b'@echo off\r\nstart "" "C:\\Py\\python.exe" -I -S ' + _OUR_APP + b"\r\n",
        b'@echo off\r\nstart "" "C:\\Py\\pythonw.exe" -I -S ' + _OUR_APP + b"\n",
        b"echo hello\r\n",
        b"\xff\xfe\x00",
    ],
)
def test_other_files_are_not_ours(data: bytes) -> None:
    assert not installer.is_our_launcher(data, APP_DIR, "ascii")


def test_app_dir_is_matched_literally() -> None:
    # 「.」要照字面比，不能當成任意字元
    dotted = f"{APPDATA}\\ToukaLocal\\ap."
    assert not installer.is_our_launcher(_our_launcher(), dotted, "ascii")


@pytest.mark.parametrize(("flag", "currently_on"), [(True, False), (False, True)])
def test_flag_decides_autostart_without_asking(flag: bool, currently_on: bool) -> None:
    asked, ask = _answers()
    assert installer.decide_autostart(flag, currently_on=currently_on, ask=ask) is flag
    assert asked == []


@pytest.mark.parametrize("currently_on", [True, False])
def test_non_interactive_keeps_autostart_as_it_is(currently_on: bool) -> None:
    assert installer.decide_autostart(None, currently_on=currently_on, ask=None) is currently_on


@pytest.mark.parametrize("currently_on", [True, False])
def test_pressing_enter_keeps_autostart_as_it_is(currently_on: bool) -> None:
    asked, ask = _answers("")
    assert installer.decide_autostart(None, currently_on=currently_on, ask=ask) is currently_on
    assert ("(Y/n)" if currently_on else "(y/N)") in asked[0]


@pytest.mark.parametrize(
    ("reply", "expected"), [("y", True), ("Y", True), ("n", False), ("N", False)]
)
def test_answer_decides_autostart(reply: str, expected: bool) -> None:
    _asked, ask = _answers(reply)
    assert installer.decide_autostart(None, currently_on=not expected, ask=ask) is expected


def test_autostart_writes_our_launcher_and_creates_the_folder(tmp_path: Path) -> None:
    target = tmp_path / "Startup" / installer._LAUNCHER_NAME
    data = _our_launcher()
    installer.apply_autostart(target, want=True, data=data, app_dir=APP_DIR, encoding="ascii")
    assert target.read_bytes() == data


def test_autostart_never_overwrites_someone_elses_file(tmp_path: Path) -> None:
    target = tmp_path / installer._LAUNCHER_NAME
    target.write_bytes(b"echo mine\r\n")
    note = installer.apply_autostart(
        target, want=True, data=_our_launcher(), app_dir=APP_DIR, encoding="ascii"
    )
    assert target.read_bytes() == b"echo mine\r\n"
    assert note


def test_turning_autostart_off_removes_only_our_launcher(tmp_path: Path) -> None:
    ours = tmp_path / "ours" / installer._LAUNCHER_NAME
    ours.parent.mkdir()
    ours.write_bytes(_our_launcher())
    installer.apply_autostart(ours, want=False, data=b"", app_dir=APP_DIR, encoding="ascii")
    assert not ours.exists()
    theirs = tmp_path / "theirs" / installer._LAUNCHER_NAME
    theirs.parent.mkdir()
    theirs.write_bytes(b"echo mine\r\n")
    installer.apply_autostart(theirs, want=False, data=b"", app_dir=APP_DIR, encoding="ascii")
    assert theirs.read_bytes() == b"echo mine\r\n"


def test_autostart_is_on_only_when_our_launcher_is_there(tmp_path: Path) -> None:
    target = tmp_path / installer._LAUNCHER_NAME
    assert not installer.has_our_launcher(target, APP_DIR, "ascii")
    target.write_bytes(b"echo mine\r\n")
    assert not installer.has_our_launcher(target, APP_DIR, "ascii")
    target.write_bytes(_our_launcher())
    assert installer.has_our_launcher(target, APP_DIR, "ascii")


def _helper_sources(source: Path, marker: str) -> None:
    for relative in installer.ALLOWED_FILES:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {marker} {relative}\n", encoding="utf-8")


def _files(folder: Path) -> dict[str, bytes]:
    files = (p for p in folder.rglob("*") if p.is_file())
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in files}


def test_failed_copy_keeps_the_old_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    _helper_sources(tmp_path / "old", "old")
    installer.copy_allowed(tmp_path / "old", app)
    before = _files(app)
    _helper_sources(tmp_path / "new", "new")
    written: list[Path] = []
    real_write = Path.write_bytes

    def flaky_write(self: Path, data: bytes) -> int:
        if len(written) == 3:
            msg = "disk full"
            raise OSError(msg)
        written.append(self)
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky_write)
    with pytest.raises(OSError, match="disk full"):
        installer.copy_allowed(tmp_path / "new", app)
    monkeypatch.undo()
    assert _files(app) == before


def test_copy_cleans_up_leftovers_from_a_failed_run(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (tmp_path / "app.new" / "junk").mkdir(parents=True)
    (tmp_path / "app.old").mkdir()
    (tmp_path / "app.old" / "x.py").write_text("x", encoding="utf-8")
    _helper_sources(tmp_path / "src", "new")
    installer.copy_allowed(tmp_path / "src", app)
    assert not (tmp_path / "app.new").exists()
    assert not (tmp_path / "app.old").exists()
    assert set(_files(app)) == set(installer.ALLOWED_FILES)


def test_replacing_the_app_does_not_follow_links(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    app = tmp_path / "app"
    app.mkdir()
    try:
        (app / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("這台電腦不能建符號連結")
    _helper_sources(tmp_path / "src", "new")
    installer.copy_allowed(tmp_path / "src", app)
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (app / "linked").exists()


@pytest.mark.skipif(not is_windows, reason="只有 Windows 有 junction")
def test_replacing_the_app_does_not_follow_junctions(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    app = tmp_path / "app"
    app.mkdir()
    _winapi.CreateJunction(str(outside), str(app / "linked"))
    _helper_sources(tmp_path / "src", "new")
    installer.copy_allowed(tmp_path / "src", app)
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_failed_swap_puts_the_old_app_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "app"
    _helper_sources(tmp_path / "old", "old")
    installer.copy_allowed(tmp_path / "old", app)
    before = _files(app)
    _helper_sources(tmp_path / "new", "new")
    real_rename = Path.rename
    calls: list[Path] = []

    def flaky_rename(self: Path, target: Path) -> Path:
        calls.append(self)
        if self.name == "app.new":
            msg = "locked"
            raise OSError(msg)
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with pytest.raises(OSError, match="locked"):
        installer.copy_allowed(tmp_path / "new", app)
    monkeypatch.undo()
    assert _files(app) == before


def test_an_old_app_that_cannot_be_removed_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "app"
    _helper_sources(tmp_path / "old", "old")
    installer.copy_allowed(tmp_path / "old", app)
    _helper_sources(tmp_path / "new", "new")
    real_remove = installer.remove_tree

    def stubborn(folder: Path) -> None:
        if folder.name == "app.old":
            msg = "in use"
            raise OSError(msg)
        real_remove(folder)

    monkeypatch.setattr(installer, "remove_tree", stubborn)
    note = installer.copy_allowed(tmp_path / "new", app)
    assert "app.old" in note
    assert b"# new" in (app / "__main__.py").read_bytes()


# --- run() 的接線：先問完、檢查完才複製；開機啟動的預設來自現有的啟動檔 ---


def _fake_plan(tmp_path: Path) -> installer.InstallPlan:
    helper = tmp_path / "helper"
    return installer.InstallPlan(
        helper_dir=str(helper),
        app_dir=str(helper / "app"),
        pythonw="C:\\Py\\pythonw.exe",
        launcher=str(helper / installer._LAUNCHER_NAME),
        touka_folder="D:\\Touka",
    )


def _wire_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, refuse: bool = False
) -> list[str]:
    events: list[str] = []
    plan = _fake_plan(tmp_path)
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", "C:\\Users\\Me\\AppData\\Roaming")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))

    def ask(prompt: str) -> str:
        events.append(f"ask:{prompt[:5]}")
        return ""

    def fake_plan(folder: str, _environ: object) -> installer.InstallPlan:
        events.append(f"plan:{folder}")
        if refuse:
            msg = "找不到這個 Touka 資料夾"
            raise installer.InstallRefused(msg)
        return plan

    def has_ours(path: Path, app_dir: str, _encoding: str = "oem") -> bool:
        events.append(f"has:{path.name}:{app_dir == plan.app_dir}")
        return True

    def copy(_source: Path, app: Path) -> str:
        events.append(f"copy:{app.name}")
        return ""

    def autostart(_path: Path, *, want: bool, data: bytes, app_dir: str) -> str:
        events.append(f"autostart:{want}")
        return ""

    monkeypatch.setattr(installer, "_console_ask", lambda: ask)
    monkeypatch.setattr(installer, "_plan", fake_plan)
    monkeypatch.setattr(installer, "has_our_launcher", has_ours)
    monkeypatch.setattr(installer, "copy_allowed", copy)
    monkeypatch.setattr(installer, "apply_autostart", autostart)
    monkeypatch.setattr(installer, "launcher_bytes", lambda _plan: b"@echo off\r\n")
    return events


def test_run_refuses_before_copying_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _wire_run(tmp_path, monkeypatch, refuse=True)
    assert installer.run(["--touka-folder", "Z:\\NoSuchTouka"]) == 1
    assert events == ["plan:Z:\\NoSuchTouka"]


def test_run_asks_everything_before_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _wire_run(tmp_path, monkeypatch)
    settings_file = tmp_path / "local" / "ToukaLocal" / "settings.json"
    save_settings(settings_file, Settings(port=8000, token=None, touka_folder="D:\\Touka"))
    assert installer.run([]) == 0
    assert [e.split(":")[0] for e in events] == ["ask", "plan", "has", "ask", "copy", "autostart"]
    assert events[2] == f"has:{installer._LAUNCHER_NAME}:True"
    # 開機啟動目前開著，按 Enter 維持原狀
    assert events[-1] == "autostart:True"
