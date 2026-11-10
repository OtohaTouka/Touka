"""Windows 呼叫的純函式、設定檔與安裝規則（ADR-0312）；真 Windows 的部分用 skipif。"""

from __future__ import annotations

import runpy
import sys
import types
from pathlib import Path

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
    monkeypatch.setattr(installer, "copy_allowed", lambda *_args: pytest.fail("不應該開始複製"))
    environ = {"LOCALAPPDATA": "C:\\Users\\Me\\AppData\\Local"}
    with pytest.raises(installer.InstallRefused):
        installer._prepare("Z:\\NoSuchTouka", environ)


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
    )
