"""把小幫手裝到使用者資料夾（ADR-0312）。

要從 Touka 資料夾以外的 clone 執行，用 repo 外的 Python 加 ``-I -S``。Touka 資料夾有掛進
容器，代理人寫得到，所以這裡拒絕從那裡安裝。``-I`` 下這支腳本匯入不到同資料夾的模組，
只用標準函式庫，也不匯入 ``touka_local``。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

ALLOWED_FILES: Final = (
    "__main__.py",
    "touka_local/__init__.py",
    "touka_local/app.py",
    "touka_local/approval.py",
    "touka_local/connection.py",
    "touka_local/display.py",
    "touka_local/signing.py",
    "touka_local/floors.py",
    "touka_local/folder_report.py",
    "touka_local/errors.py",
    "touka_local/fsops.py",
    "touka_local/grants.py",
    "touka_local/open_request.py",
    "touka_local/operations.py",
    "touka_local/paths.py",
    "touka_local/presence.py",
    "touka_local/protocol.py",
    "touka_local/revoke.py",
    "touka_local/rules.py",
    "touka_local/service.py",
    "touka_local/settings.py",
    "touka_local/view.py",
    "touka_local/winapi.py",
)

_HELPER_FOLDER: Final = "ToukaLocal"
_LAUNCHER_NAME: Final = "ToukaLocal.cmd"
_SETTINGS_NAME: Final = "settings.json"
# 和 settings.py 的 STARTUP_FOLDER 相同（安裝程式載入不到那裡，測試釘住）
_STARTUP_FOLDER: Final = ("Microsoft", "Windows", "Start Menu", "Programs", "Startup")
# 和 presence.py 的 BACKGROUND_FLAG 相同（同上，測試釘住）
_BACKGROUND_FLAG: Final = "--background"
_STAGED_SUFFIX: Final = ".new"
_OLD_SUFFIX: Final = ".old"
_DRIVE: Final = re.compile(r"[A-Za-z]:")
_UNQUOTABLE: Final = re.compile(r'["%&^!\r\n]')


class InstallRefused(Exception):
    """不符合安裝條件。"""


@dataclass(frozen=True)
class InstallPlan:
    """安裝會用到的位置。"""

    helper_dir: str
    app_dir: str
    pythonw: str
    launcher: str
    touka_folder: str


def _is_plain_absolute(path: str) -> bool:
    if not path or "\0" in path:
        return False
    pure = PureWindowsPath(path)
    return _DRIVE.fullmatch(pure.drive) is not None and pure.root == "\\"


def _inside(path: str, folder: str) -> bool:
    return PureWindowsPath(path.casefold()).is_relative_to(PureWindowsPath(folder.casefold()))


def check_flags(*, isolated: int, no_site: int) -> None:
    """要用 ``-I -S`` 執行，環境變數和 site 才影響不到安裝。"""
    if not isolated or not no_site:
        msg = "請用 python -I -S 執行"
        raise InstallRefused(msg)


def plan_install(
    *,
    source: str,
    local_app_data: str,
    python: str,
    touka_folder: str,
) -> InstallPlan:
    """算出安裝位置；來源或 Python 在 Touka 資料夾裡就拒絕。"""
    if not _is_plain_absolute(touka_folder) or not _is_plain_absolute(local_app_data):
        msg = "需要完整的本機路徑"
        raise InstallRefused(msg)
    if _inside(source, touka_folder) or _inside(python, touka_folder):
        msg = "不能從 Touka 資料夾安裝，也不能用裡面的 Python；那裡有掛進容器"
        raise InstallRefused(msg)
    helper_dir = str(PureWindowsPath(local_app_data, _HELPER_FOLDER))
    plan = InstallPlan(
        helper_dir=helper_dir,
        app_dir=str(PureWindowsPath(helper_dir, "app")),
        pythonw=str(PureWindowsPath(python).with_name("pythonw.exe")),
        launcher=str(PureWindowsPath(helper_dir, _LAUNCHER_NAME)),
        touka_folder=str(PureWindowsPath(touka_folder)),
    )
    if _UNQUOTABLE.search(plan.app_dir) or _UNQUOTABLE.search(plan.pythonw):
        msg = "路徑裡有啟動檔無法安全引用的字元"
        raise InstallRefused(msg)
    return plan


def launcher_text(plan: InstallPlan, *, background: bool = False) -> str:
    """啟動檔的內容：絕對路徑、隔離模式。開機啟動的那份帶旗標，已配對時不顯示主視窗。"""
    flag = f" {_BACKGROUND_FLAG}" if background else ""
    return f'@echo off\r\nstart "" "{plan.pythonw}" -I -S "{plan.app_dir}"{flag}\r\n'


def launcher_bytes(plan: InstallPlan, encoding: str = "oem", *, background: bool = False) -> bytes:
    """``cmd`` 用主控台字碼頁讀啟動檔；路徑寫不進去就不裝，避免產生打不開的啟動檔。"""
    try:
        return launcher_text(plan, background=background).encode(encoding)
    except UnicodeEncodeError as exc:
        msg = "路徑裡有這台電腦的主控台字碼頁寫不出來的字元"
        raise InstallRefused(msg) from exc


def check_touka_folder(folder: Path) -> None:
    """打錯路徑的話，小幫手之後會擋錯地方。"""
    if not folder.is_dir():
        msg = "找不到這個 Touka 資料夾，請確認路徑"
        raise InstallRefused(msg)


def finish_message(launcher: str, *, on_desktop: bool) -> str:
    """裝完要告訴人的話。"""
    note = "" if on_desktop else "找不到桌面資料夾，桌面上沒有捷徑，請直接開啟上面的啟動檔。\n"
    return f"已安裝。啟動檔：{launcher}\n{note}"


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _drop_link(path: Path) -> None:
    """只刪連結本身：指向資料夾的連結和 junction 要用 rmdir。"""
    if path.is_junction() or path.is_dir():
        path.rmdir()
    else:
        path.unlink()


def remove_tree(folder: Path) -> None:
    """刪資料夾，不跟著連結走；連結只刪本身，指向的內容不動。"""
    if _is_link(folder):
        _drop_link(folder)
        return
    for entry in folder.iterdir():
        if _is_link(entry):
            _drop_link(entry)
        elif entry.is_dir():
            remove_tree(entry)
        else:
            entry.unlink()
    folder.rmdir()


def _clear(path: Path) -> None:
    if _is_link(path) or path.is_dir():
        remove_tree(path)
    elif path.exists():
        path.unlink()


def copy_allowed(source: Path, app: Path) -> str:
    """只複製允許的檔案，寫好整份才換掉舊的；缺檔或中途失敗時舊的 app 還在。

    換好之後舊的刪不掉（例如防毒軟體鎖住）不算失敗，回傳要告訴人的話。
    """
    contents = {}
    for relative in ALLOWED_FILES:
        file = source / relative
        if not file.is_file():
            msg = f"缺少檔案：{relative}"
            raise InstallRefused(msg)
        contents[relative] = file.read_bytes()
    staged = app.with_name(app.name + _STAGED_SUFFIX)
    old = app.with_name(app.name + _OLD_SUFFIX)
    _clear(staged)
    _clear(old)
    for relative, data in contents.items():
        target = staged / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    had_app = _is_link(app) or app.exists()
    if had_app:
        app.rename(old)
    try:
        staged.rename(app)
    except OSError:
        if had_app:
            old.rename(app)
        raise
    if had_app:
        try:
            _clear(old)
        except OSError:
            return f"舊的程式刪不掉，留在 {old}，可以之後自己刪。\n"
    return ""


def _read_settings(file: Path) -> object:
    """小幫手當機在換檔中途時，設定只留在暫存檔。"""
    for candidate in (file, file.with_name(f"{file.name}.new")):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return {}
    return {}


def current_touka_folder(file: Path) -> str | None:
    """上次安裝記下的 Touka 資料夾；重裝時按 Enter 沿用。"""
    current = _read_settings(file)
    folder = current.get("touka_folder") if isinstance(current, dict) else None
    return folder if isinstance(folder, str) and folder else None


def clean_folder_input(raw: str) -> str:
    """去掉前後空白，以及檔案總管「複製為路徑」會帶的一組前後雙引號（和設定頁相同）。"""
    trimmed = raw.strip()
    quoted = len(trimmed) >= 2 and trimmed.startswith('"') and trimmed.endswith('"')
    return (trimmed[1:-1] if quoted else trimmed).strip()


def resolve_touka_folder(
    given: str | None, current: str | None, ask: Callable[[str], str] | None
) -> str:
    """有指定就用指定的；能問就問（Enter 沿用上次）；不能問就沿用上次，沒有上次就不裝。"""
    if given is not None:
        return clean_folder_input(given)
    if ask is None:
        if current:
            return current
        msg = "請用 --touka-folder 指定 Touka 資料夾"
        raise InstallRefused(msg)
    hint = f"（直接按 Enter 沿用 {current}）" if current else ""
    answer = clean_folder_input(ask(f"Touka 資料夾在哪？{hint}："))
    if answer:
        return answer
    if current:
        return current
    msg = "沒有輸入 Touka 資料夾"
    raise InstallRefused(msg)


def startup_launcher(app_data: str) -> str:
    """開機自動啟動的啟動檔放在使用者自己的「啟動」資料夾。"""
    return str(PureWindowsPath(app_data, *_STARTUP_FOLDER, _LAUNCHER_NAME))


def is_our_launcher(data: bytes, app_dir: str, encoding: str = "oem") -> bool:
    """整份內容要是我們產生的樣子、啟動這次的 app 資料夾；Python 的位置可以不同（舊版）。

    帶不帶開機啟動旗標都算（0.3.0 以前的沒有），其他參數不算。
    """
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return False
    pattern = (
        r'@echo off\r\nstart "" "[^"\r\n]+\\pythonw\.exe" -I -S "'
        + re.escape(app_dir)
        + r'"(?: '
        + re.escape(_BACKGROUND_FLAG)
        + r")?\r\n"
    )
    return re.fullmatch(pattern, text) is not None


def has_our_launcher(path: Path, app_dir: str, encoding: str = "oem") -> bool:
    """開機啟動目前開著嗎：那裡有我們的啟動檔才算。"""
    if _is_link(path) or not path.is_file():
        return False
    return is_our_launcher(path.read_bytes(), app_dir, encoding)


def decide_autostart(
    flag: bool | None, *, currently_on: bool, ask: Callable[[str], str] | None
) -> bool:
    """有指定就照指定；不能問就維持原狀；問的時候預設也是維持原狀（升級一路按 Enter 不會改掉）。"""
    if flag is not None:
        return flag
    if ask is None:
        return currently_on
    choices = "(Y/n)" if currently_on else "(y/N)"
    answer = ask(f"要開機自動啟動小幫手嗎？{choices}：").strip().casefold()
    if answer in {"y", "yes"}:
        return True
    if answer in {"n", "no"}:
        return False
    return currently_on


def apply_autostart(
    path: Path, *, want: bool, data: bytes, app_dir: str, encoding: str = "oem"
) -> str:
    """寫入或移除開機啟動檔；別人的同名檔不蓋也不刪。回傳要告訴人的話。"""
    ours = has_our_launcher(path, app_dir, encoding)
    taken = (_is_link(path) or path.exists()) and not ours
    if want:
        if taken:
            return f"開機啟動資料夾裡已經有別的 {_LAUNCHER_NAME}，沒有覆蓋，請自己確認：{path}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return "已設定開機自動啟動。\n"
    if ours:
        path.unlink()
        return "已取消開機自動啟動。\n"
    return ""


def update_settings(file: Path, touka_folder: str) -> None:
    """記下 Touka 資料夾，保留已有的配對和連接埠。"""
    current = _read_settings(file)
    if not isinstance(current, dict):
        current = {}
    payload = {
        "port": current.get("port", 8000),
        "token": current.get("token"),
        "touka_folder": touka_folder,
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _source() -> Path:
    return Path(__file__).resolve().parent


def _plan(touka_folder: str, environ: Mapping[str, str]) -> InstallPlan:
    check_flags(isolated=sys.flags.isolated, no_site=sys.flags.no_site)
    plan = plan_install(
        source=str(_source()),
        local_app_data=environ["LOCALAPPDATA"],
        python=str(Path(sys.executable).resolve()),
        touka_folder=touka_folder,
    )
    check_touka_folder(Path(plan.touka_folder))
    if not Path(plan.pythonw).is_file():
        msg = "找不到 pythonw.exe"
        raise InstallRefused(msg)
    launcher_bytes(plan)
    return plan


def _console_ask() -> Callable[[str], str] | None:
    """在主控台裡才問；被別的程式呼叫（沒有終端機）時不問，免得卡住。"""
    return input if sys.stdin is not None and sys.stdin.isatty() else None


def run(argv: list[str]) -> int:
    """安裝。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--touka-folder")
    autostart = parser.add_mutually_exclusive_group()
    autostart.add_argument("--autostart", dest="autostart", action="store_const", const=True)
    autostart.add_argument("--no-autostart", dest="autostart", action="store_const", const=False)
    arguments = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stderr.write("小幫手只支援 Windows\n")
        return 1
    environ = os.environ  # noqa: ENV_SSOT_VIOLATION
    ask = _console_ask()
    try:
        settings_file = Path(environ["LOCALAPPDATA"]) / _HELPER_FOLDER / _SETTINGS_NAME
        startup = Path(startup_launcher(environ["APPDATA"]))
        touka_folder = resolve_touka_folder(
            arguments.touka_folder, current_touka_folder(settings_file), ask
        )
        plan = _plan(touka_folder, environ)
        want = decide_autostart(
            arguments.autostart, currently_on=has_our_launcher(startup, plan.app_dir), ask=ask
        )
        note = copy_allowed(_source(), Path(plan.app_dir))
        update_settings(Path(plan.helper_dir) / _SETTINGS_NAME, plan.touka_folder)
        data = launcher_bytes(plan)
        startup_data = launcher_bytes(plan, background=True)
        Path(plan.launcher).write_bytes(data)
        desktop = Path(environ["USERPROFILE"]) / "Desktop"
        on_desktop = desktop.is_dir()
        # 新版已經換上；桌面與開機啟動檔寫不進去（例如受控資料夾存取）只說明，不算失敗
        if on_desktop:
            try:
                (desktop / _LAUNCHER_NAME).write_bytes(data)
            except OSError as exc:
                note += f"桌面的啟動檔寫不進去（{exc}），請直接開啟上面的啟動檔。\n"
        try:
            note += apply_autostart(startup, want=want, data=startup_data, app_dir=plan.app_dir)
        except OSError as exc:
            note += f"開機自動啟動沒有改成功：{exc}\n"
    except (InstallRefused, KeyError, OSError) as exc:
        sys.stderr.write(f"沒有安裝：{exc}\n")
        return 1
    except EOFError:
        sys.stderr.write("沒有安裝：沒有收到回答\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("沒有安裝：已取消\n")
        return 1
    sys.stdout.write(finish_message(plan.launcher, on_desktop=on_desktop) + note)
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
