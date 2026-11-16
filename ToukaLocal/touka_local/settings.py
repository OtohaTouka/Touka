"""小幫手的設定檔與這台電腦的位置（ADR-0312）。"""

from __future__ import annotations

import json
import math
import ntpath
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from touka_local.connection import check_port
from touka_local.errors import Refused
from touka_local.fsops import (
    durable_write_text,
    previous_path,
    read_durable_text,
    temporary_path,
)
from touka_local.grants import Environment
from touka_local.paths import is_plain_absolute, join

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

DEFAULT_PORT: Final = 8000
HELPER_FOLDER_NAME: Final = "ToukaLocal"
# 和 install.py 的 _LAUNCHER_NAME 相同（安裝程式載入不到這裡，測試釘住）
LAUNCHER_NAME: Final = "ToukaLocal.cmd"
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
        code_paths=(
            ntpath.dirname(sys.executable),
            sys.base_prefix,
            join(values["USERPROFILE"], "Desktop", LAUNCHER_NAME),
        ),
        touka_folder=str(touka_folder),
    )


# --- 照 Touka 的模式核准（ADR-0317）：讀不懂一律往安全的那邊算 ---

DECISION_KEY_FILE: Final = "decision_key"
MODE_FOLLOWING_FILE: Final = "mode_following.json"
MODE_RESET_FILE: Final = "mode_reset.json"
_KEY_HEX: Final = re.compile(r"[0-9a-f]{64}")


def load_decision_key(file: Path) -> bytes | None:
    """配對時拿到的鑰匙；沒有或格式不對就沒有，這時每份清單都跳視窗。"""
    try:
        text = read_durable_text(file).strip()
    except (OSError, ValueError):
        return None
    return bytes.fromhex(text) if _KEY_HEX.fullmatch(text) else None


def save_decision_key(file: Path, key: bytes | None) -> None:
    """存鑰匙；None 是刪掉（換成沒有鑰匙的舊配對碼時）。"""
    if key is not None:
        durable_write_text(file, key.hex())
        return
    for path in (file, temporary_path(file), previous_path(file)):
        path.unlink(missing_ok=True)


def load_mode_following(file: Path) -> bool:
    """「照 Touka 的模式核准」有沒有打開。讀不到、壞掉、看不懂都當作關掉。"""
    try:
        payload = json.loads(read_durable_text(file))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and set(payload) == {"enabled"} and payload["enabled"] is True


def save_mode_following(file: Path, *, enabled: bool) -> None:
    """寫開關。"""
    durable_write_text(file, json.dumps({"enabled": enabled}))


def load_mode_reset(file: Path) -> float | None:
    """人在主視窗按「重新計算」的時間；讀不到就當作沒有重設。"""
    try:
        payload = json.loads(read_durable_text(file))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"reset_at"}:
        return None
    value = payload["reset_at"]
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def save_mode_reset(file: Path, at: float) -> None:
    """記下重新計算的時間。"""
    durable_write_text(file, json.dumps({"reset_at": at}))
