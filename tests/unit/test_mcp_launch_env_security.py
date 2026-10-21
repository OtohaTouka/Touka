"""缺口 2：MCP stdio 伺服器的啟動環境改成白名單底（iso-c1 U2）.

修補前：已安裝的 stdio 伺服器啟動時拿到 ``os.environ.copy()``（``settings.get_full_env()``），
含 ``TOUKA_AUTH_SECRET``、``DISPATCH_TOKEN``、``HEARTBEAT_TOKEN`` 與全部廠商金鑰；創建引擎試跑
走 ``get_clean_env`` 黑名單，漏得掉 ``*_PASS``、``DATABASE_URL`` 一類。

修補後：兩條路徑共用 ``LaunchEnvironmentPolicy`` 的白名單常數。
- 正式：白名單內的父環境＋條目自己的 env（含哨兵解析；名稱有 TOKEN／KEY 的值照給）
  ＋``TOOL_BASE_URL``。
- 試跑（clean）：白名單內的父環境＋spec 宣告的 env（不解析哨兵，SP7）。
- 白名單只補 MCP SDK ``get_default_environment()`` 沒帶的；Windows 名稱比對不分大小寫。

斷言都在 ``StdioServerParameters.env`` 這層（SDK 合併的部分看不到）；父環境用假的 dict，
不讀真的 ``.env``、不啟動任何伺服器。只擋「環境變數順手帶走」，擋不住讀 ``.env`` 檔的套件。
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS
from src.config import get_app_data_dir, settings
from src.protocols.mcp import client as mcp_client
from src.protocols.mcp import environment as environment_module
from src.protocols.mcp.environment import LAUNCH_ENV_ALLOWLIST, LaunchEnvironmentPolicy
from src.protocols.registry import ProtocolServerConfig
from src.services.creation import smoke as smoke_module
from src.utils.mcp_sentinels import APPDATA_SENTINEL

_TOUKA_SECRETS = {
    "TOUKA_AUTH_SECRET": "touka-secret",
    "DISPATCH_TOKEN": "dispatch-secret",
    "HEARTBEAT_TOKEN": "heartbeat-secret",
}
_PROVIDER_KEYS = {
    "OPENAI_API_KEY": "parent-openai",
    "ANTHROPIC_API_KEY": "parent-anthropic",
    "GEMINI_API_KEY": "parent-gemini",
    "NOTION_TOKEN": "parent-notion",
}
_OTHER_SECRETS = {
    "STRIPE_SK": "parent-stripe",
    "DATABASE_URL": "postgres://user:pass@db/x",
    "SMTP_PASS": "parent-smtp",
    "PYTHONPATH": "parent-pythonpath",
}
_ALLOWED_SYSTEM = {
    "LANG": "C.UTF-8",
    "TZ": "Asia/Taipei",
    "TMPDIR": "probe-tmpdir",
    "UV_PYTHON_DOWNLOADS": "never",
}
_PARENT = {**_TOUKA_SECRETS, **_PROVIDER_KEYS, **_OTHER_SECRETS, **_ALLOWED_SYSTEM}
_NEVER_INHERITED = {*_TOUKA_SECRETS, *_OTHER_SECRETS, "ANTHROPIC_API_KEY", "GEMINI_API_KEY"}


@pytest.fixture
def parent_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """假的父行程環境（含假金鑰）；POSIX 大小寫語意，個別測試再切 Windows。"""
    monkeypatch.setattr(type(settings), "get_full_env", lambda _self: dict(_PARENT))
    monkeypatch.setattr(environment_module, "_CASE_INSENSITIVE_NAMES", False)
    return _PARENT


async def _launched_env(monkeypatch: pytest.MonkeyPatch, config: ProtocolServerConfig) -> dict:
    """走正式連線路徑，攔下送進 SDK 的 ``StdioServerParameters.env``。"""
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params: Any):
        captured["params"] = params
        yield ("read", "write")

    class _Session:
        async def send_discover(self, _version: str) -> dict[str, Any]:
            return {
                "cacheScope": "public",
                "capabilities": {},
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "ttlMs": 0,
            }

        def adopt(self, _result: Any) -> None:
            return None

    class _Client:
        def __init__(self, target: Any, **_kwargs: Any) -> None:
            self._target = target
            self.session = _Session()

        async def __aenter__(self) -> _Client:
            await self._target.__aenter__()
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return bool(await self._target.__aexit__(*exc))

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "Client", _Client)
    _client, stack = await mcp_client.connect_mcp_server(config)
    await stack.aclose()
    return dict(captured["params"].env)


_ENTRY_SECRETS = {"NOTION_TOKEN": "entry-notion", "OPENAI_API_KEY": "entry-openai"}


def _hub_entry() -> ProtocolServerConfig:
    """Hub 形狀的條目：自己的 env 明列 TOKEN／KEY 名稱的值與哨兵。"""
    return ProtocolServerConfig(
        command="npx",
        args=["-y", "@notionhq/notion-mcp-server"],
        env={**_ENTRY_SECRETS, "DATA_DIR": APPDATA_SENTINEL},
    )


# ── 正式安裝的伺服器 ──────────────────────────────────────────────────────────────


async def test_production_launch_env_drops_touka_and_provider_secrets(monkeypatch, parent_env):
    env = await _launched_env(monkeypatch, _hub_entry())
    assert _NEVER_INHERITED.isdisjoint(env)


async def test_production_launch_env_keeps_entry_env_tool_url_and_allowlisted_system(
    monkeypatch, parent_env
):
    env = await _launched_env(monkeypatch, _hub_entry())
    for name, value in _ENTRY_SECRETS.items():
        assert env[name] == value  # 條目自己明列的 TOKEN／KEY 照給（蓋過父環境同名值）
    assert env["DATA_DIR"] == str(get_app_data_dir())  # 正式路徑照舊解析哨兵
    assert env["TOOL_BASE_URL"].startswith("http://")
    for name, value in _ALLOWED_SYSTEM.items():
        assert env[name] == value


async def test_production_entry_without_env_gets_only_allowlist_and_tool_url(
    monkeypatch, parent_env
):
    env = await _launched_env(monkeypatch, ProtocolServerConfig(command="uvx", args=["probe"]))
    assert set(env) == {*_ALLOWED_SYSTEM, "TOOL_BASE_URL"}


# ── 創建引擎試跑（clean）────────────────────────────────────────────────────────


def test_clean_uses_the_same_allowlist_and_keeps_sentinels_unresolved(parent_env):
    env = LaunchEnvironmentPolicy.clean({"CANDIDATE": "declared", "DATA_DIR": APPDATA_SENTINEL})
    assert env == {**_ALLOWED_SYSTEM, "CANDIDATE": "declared", "DATA_DIR": APPDATA_SENTINEL}


def test_creation_smoke_launch_env_is_the_allowlist_plus_spec_env(parent_env):
    env = smoke_module._build_launch_env({"DECLARED": "v"})
    assert env == {**_ALLOWED_SYSTEM, "DECLARED": "v"}
    assert "PYTHONPATH" not in env


def test_clean_has_no_ignored_base_factory_parameter():
    assert list(inspect.signature(LaunchEnvironmentPolicy.clean).parameters) == ["declared_env"]


# ── 白名單本身 ────────────────────────────────────────────────────────────────


def test_allowlist_only_adds_what_the_sdk_does_not_inherit():
    assert LAUNCH_ENV_ALLOWLIST.isdisjoint(DEFAULT_INHERITED_ENV_VARS)
    assert {"LANG", "TZ", "TMPDIR", "UV_PYTHON_DOWNLOADS"} <= LAUNCH_ENV_ALLOWLIST
    assert all(name == name.upper() for name in LAUNCH_ENV_ALLOWLIST)
    assert not any(
        marker in name
        for name in LAUNCH_ENV_ALLOWLIST
        for marker in ("SECRET", "TOKEN", "KEY", "PASS", "AUTH", "PROXY")
    )


# ── 隔離路徑的啟動器環境（C2-3 U3e-1）──────────────────────────────────────────


def test_launcher_base_is_the_sdk_default_plus_the_allowlist_only(monkeypatch, parent_env):
    """啟動器（landlock 啟動器、執行器 relay）自己的環境：SDK 預設＋允許清單；沒有任何金鑰，也沒有
    條目的 env 或工具網址（那些在 exec_env，只經 spec 交給目標）."""
    sdk_default = {"PATH": "sdk-path", "HOME": "sdk-home"}
    monkeypatch.setattr(environment_module, "get_default_environment", lambda: dict(sdk_default))
    env = LaunchEnvironmentPolicy.launcher_base()
    assert env == {**sdk_default, **_ALLOWED_SYSTEM}
    assert set(env).isdisjoint({*_TOUKA_SECRETS, *_PROVIDER_KEYS, *_OTHER_SECRETS})
    assert "TOOL_BASE_URL" not in env


def test_launcher_base_takes_no_entry_env():
    assert list(inspect.signature(LaunchEnvironmentPolicy.launcher_base).parameters) == []


@pytest.mark.parametrize(("case_insensitive", "expected"), [(True, True), (False, False)])
def test_windows_names_match_case_insensitively(monkeypatch, case_insensitive, expected):
    mixed = {"Tz": "Asia/Taipei", "uv_python_downloads": "never", "Touka_Auth_Secret": "s"}
    monkeypatch.setattr(type(settings), "get_full_env", lambda _self: dict(mixed))
    monkeypatch.setattr(environment_module, "_CASE_INSENSITIVE_NAMES", case_insensitive)
    env = LaunchEnvironmentPolicy.clean(None)
    assert ("Tz" in env and "uv_python_downloads" in env) is expected
    assert "Touka_Auth_Secret" not in env
