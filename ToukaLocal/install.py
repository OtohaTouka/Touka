"""把小幫手裝到使用者資料夾（ADR-0311）。

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
    from collections.abc import Mapping

ALLOWED_FILES: Final = (
    "__main__.py",
    "touka_local/__init__.py",
    "touka_local/app.py",
    "touka_local/approval.py",
    "touka_local/connection.py",
    "touka_local/errors.py",
    "touka_local/fsops.py",
    "touka_local/grants.py",
    "touka_local/operations.py",
    "touka_local/paths.py",
    "touka_local/protocol.py",
    "touka_local/rules.py",
    "touka_local/service.py",
    "touka_local/settings.py",
    "touka_local/view.py",
    "touka_local/winapi.py",
)

_HELPER_FOLDER: Final = "ToukaLocal"
_LAUNCHER_NAME: Final = "ToukaLocal.cmd"
_SETTINGS_NAME: Final = "settings.json"
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


def launcher_text(plan: InstallPlan) -> str:
    """啟動檔的內容：絕對路徑、隔離模式。"""
    return f'@echo off\r\nstart "" "{plan.pythonw}" -I -S "{plan.app_dir}"\r\n'


def launcher_bytes(plan: InstallPlan, encoding: str = "oem") -> bytes:
    """``cmd`` 用主控台字碼頁讀啟動檔；路徑寫不進去就不裝，避免產生打不開的啟動檔。"""
    try:
        return launcher_text(plan).encode(encoding)
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


def copy_allowed(source: Path, app: Path) -> None:
    """只複製允許的檔案，舊的先清掉。缺檔就什麼都不動。"""
    contents = {}
    for relative in ALLOWED_FILES:
        file = source / relative
        if not file.is_file():
            msg = f"缺少檔案：{relative}"
            raise InstallRefused(msg)
        contents[relative] = file.read_bytes()
    if app.is_dir():
        for stale in app.rglob("*"):
            if stale.is_file():
                stale.unlink()
    for relative, data in contents.items():
        target = app / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


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


def _prepare(touka_folder: str, environ: Mapping[str, str]) -> InstallPlan:
    check_flags(isolated=sys.flags.isolated, no_site=sys.flags.no_site)
    source = Path(__file__).resolve().parent
    plan = plan_install(
        source=str(source),
        local_app_data=environ["LOCALAPPDATA"],
        python=str(Path(sys.executable).resolve()),
        touka_folder=touka_folder,
    )
    check_touka_folder(Path(plan.touka_folder))
    if not Path(plan.pythonw).is_file():
        msg = "找不到 pythonw.exe"
        raise InstallRefused(msg)
    launcher_bytes(plan)
    copy_allowed(source, Path(plan.app_dir))
    return plan


def run(argv: list[str]) -> int:
    """安裝。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--touka-folder", required=True)
    arguments = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stderr.write("小幫手只支援 Windows\n")
        return 1
    environ = os.environ  # noqa: ENV_SSOT_VIOLATION
    try:
        plan = _prepare(arguments.touka_folder, environ)
    except (InstallRefused, KeyError) as exc:
        sys.stderr.write(f"沒有安裝：{exc}\n")
        return 1
    update_settings(Path(plan.helper_dir) / _SETTINGS_NAME, plan.touka_folder)
    data = launcher_bytes(plan)
    Path(plan.launcher).write_bytes(data)
    desktop = Path(environ["USERPROFILE"]) / "Desktop"
    on_desktop = desktop.is_dir()
    if on_desktop:
        (desktop / _LAUNCHER_NAME).write_bytes(data)
    sys.stdout.write(finish_message(plan.launcher, on_desktop=on_desktop))
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
