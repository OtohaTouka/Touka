"""MCP 子進程啟動環境政策（production 與 Creation clean 明確分流）。

兩條路徑共用同一個白名單底（``LAUNCH_ENV_ALLOWLIST``）：父行程環境只有白名單內的名稱會傳下去，
Touka 金鑰與廠商金鑰一律不繼承；伺服器需要的值寫在條目自己的 ``env``（名稱含 TOKEN／KEY 照給）。
MCP SDK 的 ``stdio_client`` 啟動時會再合併 ``get_default_environment()``（PATH、SYSTEMROOT、
HOME 一類），所以白名單只補 SDK 沒帶的。這只擋「環境變數順手帶走」，擋不住會去讀 ``.env`` 檔的
惡意套件（那要靠隔離執行環境）。

隔離路徑（iso-c2 C2-3）不經 SDK 的 ``stdio_client``：啟動器（landlock 啟動器、執行器 relay）
自己的環境由 ``launcher_base`` 組（SDK 預設＋白名單），條目的 env 只放 spec 的 ``exec_env``，
套完限制才交給伺服器。完整的父行程環境只在這一層讀，隔離傳輸與判定只拿組好的 dict。
"""

from __future__ import annotations

import sys
from typing import Final

from mcp.client.stdio import get_default_environment
from src.config import settings
from src.utils.mcp_sentinels import resolve_env_sentinels

# 執行必需、不含機密的系統變數（禁令 18：模組常數）。不列代理伺服器設定（可能夾帶帳密）與
# 任何 uv／npm 的 index、registry 設定（可能夾帶 token）；要用就寫進該伺服器自己的 env。
LAUNCH_ENV_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        # 語系與時區
        "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
        # 暫存目錄（Windows 的 TEMP 由 SDK 帶）
        "TMPDIR", "TMP",
        # Windows 系統目錄與 cmd（SDK 只帶 SYSTEMROOT 等）
        "COMSPEC", "WINDIR", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "PROGRAMW6432", "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS", "OS",
        # POSIX 快取與設定目錄（uvx／npx 的套件快取）
        "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        # uv／npm 的非機密設定
        "UV_PYTHON_DOWNLOADS", "UV_CACHE_DIR", "UV_TOOL_DIR", "UV_PYTHON_INSTALL_DIR",
        "NPM_CONFIG_CACHE", "NPM_CONFIG_PREFIX",
    }
)  # fmt: skip

# Windows 的環境變數名稱不分大小寫；POSIX 區分大小寫。
_CASE_INSENSITIVE_NAMES = sys.platform == "win32"


def _allowlisted_parent_env() -> dict[str, str]:
    """父行程環境裡白名單內的變數；其餘（含全部金鑰）一律不繼承。"""
    return {
        name: value
        for name, value in settings.get_full_env().items()
        if (name.upper() if _CASE_INSENSITIVE_NAMES else name) in LAUNCH_ENV_ALLOWLIST
    }


class LaunchEnvironmentPolicy:
    """啟動環境 SSOT；呼叫端不得自行拼接 parent env。"""

    @staticmethod
    def production(
        declared_env: dict[str, str] | None,
        *,
        tool_base_url: str,
    ) -> dict[str, str]:
        """正式已安裝 server：白名單底＋條目 env（解析哨兵）＋Touka tool URL。"""
        env = _allowlisted_parent_env()
        if declared_env:
            env.update(resolve_env_sentinels(declared_env))
        env["TOOL_BASE_URL"] = tool_base_url
        return env

    @staticmethod
    def launcher_base() -> dict[str, str]:
        """隔離路徑的啟動器環境：SDK 預設（同 ``stdio_client``）＋白名單；條目 env 不在這裡."""
        return {**get_default_environment(), **_allowlisted_parent_env()}

    @staticmethod
    def clean(declared_env: dict[str, str] | None) -> dict[str, str]:
        """Creation candidate：同一個白名單底＋spec 明示 env；不解析哨兵（SP7）。"""
        return {**_allowlisted_parent_env(), **(declared_env or {})}
