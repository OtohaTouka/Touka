"""小幫手的設定檔與這台電腦的位置（ADR-0311）。"""

from __future__ import annotations

import json
import ntpath
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from touka_local.connection import check_port
from touka_local.errors import Refused
from touka_local.fsops import durable_write_text, read_durable_text
from touka_local.grants import Environment
from touka_local.paths import is_plain_absolute, join

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

DEFAULT_PORT: Final = 8000
HELPER_FOLDER_NAME: Final = "ToukaLocal"
_FIELDS: Final = frozenset({"port", "token", "touka_folder"})
_REQUIRED_LOCATIONS: Final = (
    "USERPROFILE",
    "WINDIR",
    "ProgramFiles",
    "ProgramData",
    "LOCALAPPDATA",
    "APPDATA",
)
_OPTIONAL_PROGRAM_FILES: Final = ("ProgramFiles(x86)", "ProgramW6432")


@dataclass(frozen=True)
class Settings:
    """連接埠、配對碼、Touka 資料夾。"""

    port: int = DEFAULT_PORT
    token: str | None = None
    touka_folder: str | None = None


def load_settings(file: Path) -> Settings:
    """讀設定，壞掉就用預設值（沒有配對、不知道 Touka 在哪）。"""
    try:
        payload = json.loads(read_durable_text(file))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(payload, dict) or not set(payload) <= _FIELDS:
        return Settings()
    port = payload.get("port", DEFAULT_PORT)
    token = payload.get("token")
    touka_folder = payload.get("touka_folder")
    try:
        check_port(port)
    except Refused:
        return Settings()
    if not (token is None or isinstance(token, str)):
        return Settings()
    if not (touka_folder is None or isinstance(touka_folder, str)):
        return Settings()
    return Settings(port=port, token=token, touka_folder=touka_folder)


def save_settings(file: Path, settings: Settings) -> None:
    """寫設定。"""
    payload = {
        "port": settings.port,
        "token": settings.token,
        "touka_folder": settings.touka_folder,
    }
    durable_write_text(file, json.dumps(payload, ensure_ascii=False, indent=2))


def load_environment(environ: Mapping[str, str], touka_folder: str | None) -> Environment:
    """讀這台電腦的位置；少任何一個都不啟動，寧可不開放也不要漏擋。"""
    values = {key: environ.get(key, "") for key in _REQUIRED_LOCATIONS}
    if not all(is_plain_absolute(value) for value in values.values()):
        raise Refused("bad_environment")
    if not is_plain_absolute(touka_folder):
        raise Refused("bad_environment")
    extras = (environ.get(key, "") for key in _OPTIONAL_PROGRAM_FILES)
    program_files = tuple(
        dict.fromkeys((values["ProgramFiles"], *(e for e in extras if is_plain_absolute(e))))
    )
    return Environment(
        user_profile=values["USERPROFILE"],
        windows_dir=values["WINDIR"],
        program_files=program_files,
        program_data=values["ProgramData"],
        app_data=values["APPDATA"],
        local_app_data=values["LOCALAPPDATA"],
        helper_dir=join(values["LOCALAPPDATA"], HELPER_FOLDER_NAME),
        python_dir=ntpath.dirname(sys.executable),
        touka_folder=str(touka_folder),
    )
