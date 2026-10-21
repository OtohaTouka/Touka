"""ADR-0067：ToukaDispatch 自動召喚外部 AI（dispatch_hooks.json CaC）測試。

驗證：
  - _load_dispatch_hooks：讀白名單，缺檔/壞檔/非 dict 回 {} (fail-closed)
  - _build_child_env：whitelist 過濾（全域 DISPATCH_TOKEN 主金鑰不洩漏、密鑰擋下、
    PATH 保留、mission_token/board/url 正確；DISPATCH_URL 指向 modern /mcp）
  - _invoke_external_ai：command 白名單 + shell=False + DEVNULL；executor 不在 hooks→skip；
    Popen 拋錯不中斷；mission_token 不進 command
  - dispatch_initialize_mission with auto_invoke：觸發/向下相容/授權邊界
  - 靜態：auto_invoke 參數存在、server.py 無 shell=True

DISPATCH_TOKEN 設定後延遲匯入 server（比照 test_dispatch_mission_token.py）。
"""
# ruff: noqa: S105, E501, TRY003  # 測試：假 DISPATCH_TOKEN 值 + 長 JSON + mock 拋錯訊息

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_DISPATCH_SERVER = Path(__file__).resolve().parents[2] / "ToukaDispatch" / "server.py"


def _write_hooks(tmp_path, content="{}"):
    f = tmp_path / "dispatch_hooks.json"
    f.write_text(content, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# A. _load_dispatch_hooks
# ---------------------------------------------------------------------------


class TestLoadHooks:
    def test_valid_returns_dict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        from ToukaDispatch import server as ds

        f = _write_hooks(tmp_path, '{"on_mission_created": {"claude-code": {"command": ["claude"]}}}')
        monkeypatch.setattr(ds, "_HOOKS_PATH", f)
        data = ds._load_dispatch_hooks()
        assert "on_mission_created" in data

    def test_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        from ToukaDispatch import server as ds

        monkeypatch.setattr(ds, "_HOOKS_PATH", tmp_path / "nope.json")
        assert ds._load_dispatch_hooks() == {}

    def test_invalid_json_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        from ToukaDispatch import server as ds

        monkeypatch.setattr(ds, "_HOOKS_PATH", _write_hooks(tmp_path, "{ broken"))
        assert ds._load_dispatch_hooks() == {}

    def test_non_dict_root_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        from ToukaDispatch import server as ds

        monkeypatch.setattr(ds, "_HOOKS_PATH", _write_hooks(tmp_path, "[1, 2, 3]"))
        assert ds._load_dispatch_hooks() == {}


# ---------------------------------------------------------------------------
# B. _build_child_env（安全核心）
# ---------------------------------------------------------------------------


class TestBuildChildEnv:
    def test_global_master_token_not_leaked(self, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "GLOBAL_MASTER_SECRET")
        from ToukaDispatch import server as ds

        env = ds._build_child_env("mission-tok-123", "mission-id-456")
        assert env["DISPATCH_TOKEN"] == "mission-tok-123"
        assert "GLOBAL_MASTER_SECRET" not in env.values()

    def test_secrets_blocked_by_whitelist(self, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        monkeypatch.setenv("TOUKA_AUTH_SECRET", "jwt-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-xxx")
        monkeypatch.setenv("DB_PASSWORD", "pw")
        monkeypatch.setenv("STRIPE_SK", "no-keyword-secret")
        monkeypatch.setenv("HEARTBEAT_TOKEN", "hb")
        from ToukaDispatch import server as ds

        env = ds._build_child_env("mt", "mid")
        for leaked in ("TOUKA_AUTH_SECRET", "OPENAI_API_KEY", "DB_PASSWORD",
                       "STRIPE_SK", "HEARTBEAT_TOKEN"):
            assert leaked not in env, f"{leaked} 不應傳給外部進程 (whitelist 預設拒絕)"

    def test_path_preserved(self, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        from ToukaDispatch import server as ds

        env = ds._build_child_env("mt", "mid")
        assert env.get("PATH") == "/usr/bin:/bin"

    def test_dispatch_vars_set_correctly(self, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        from ToukaDispatch import server as ds

        env = ds._build_child_env("mt-token", "mid-board")
        assert env["DISPATCH_BOARD"] == "mid-board"
        assert env["DISPATCH_URL"] == f"http://localhost:{ds._DISPATCH_PORT}/mcp"


# ---------------------------------------------------------------------------
# C. _invoke_external_ai
# ---------------------------------------------------------------------------


class TestInvokeExternalAI:
    def _setup_hooks(self, ds, tmp_path, monkeypatch, command='["claude", "-p", ""]'):
        f = _write_hooks(
            tmp_path, '{"on_mission_created": {"claude-code": {"command": ' + command + "}}}"
        )
        monkeypatch.setattr(ds, "_HOOKS_PATH", f)

    def test_popen_command_and_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        from ToukaDispatch import server as ds

        self._setup_hooks(ds, tmp_path, monkeypatch)
        calls = {}
        monkeypatch.setattr(ds.subprocess, "Popen",
                            lambda command, **kw: calls.update(command=command, kw=kw) or object())
        result = ds._invoke_external_ai("claude-code", "mid", "mtoken")
        assert calls["command"] == ["claude", "-p", ""]
        assert calls["kw"]["env"]["DISPATCH_TOKEN"] == "mtoken"
        assert "🚀" in result

    def test_popen_safety_kwargs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        from ToukaDispatch import server as ds

        self._setup_hooks(ds, tmp_path, monkeypatch)
        calls = {}
        monkeypatch.setattr(ds.subprocess, "Popen",
                            lambda command, **kw: calls.update(kw) or object())
        ds._invoke_external_ai("claude-code", "mid", "mt")
        assert calls["stdout"] == ds.subprocess.DEVNULL
        assert calls["stdin"] == ds.subprocess.DEVNULL
        assert calls["close_fds"] is True
        assert calls.get("shell", False) is False

    def test_unknown_executor_skips(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        from ToukaDispatch import server as ds

        monkeypatch.setattr(ds, "_HOOKS_PATH", _write_hooks(tmp_path, '{"on_mission_created": {}}'))
        called = []
        monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: called.append(1))
        result = ds._invoke_external_ai("aider", "mid", "mt")
        assert "skipped" in result.lower()
        assert not called

    def test_popen_oserror_no_raise(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        from ToukaDispatch import server as ds

        self._setup_hooks(ds, tmp_path, monkeypatch)

        def _raise(*a, **k):
            raise FileNotFoundError("claude not found")

        monkeypatch.setattr(ds.subprocess, "Popen", _raise)
        result = ds._invoke_external_ai("claude-code", "mid", "mt")
        assert "failed" in result.lower()

    def test_token_not_in_command(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "x")
        from ToukaDispatch import server as ds

        self._setup_hooks(ds, tmp_path, monkeypatch)
        calls = {}
        monkeypatch.setattr(ds.subprocess, "Popen",
                            lambda command, **kw: calls.update(command=command) or object())
        ds._invoke_external_ai("claude-code", "mid", "SECRET_MISSION_TOKEN")
        assert all("SECRET_MISSION_TOKEN" not in str(arg) for arg in calls["command"])


# ---------------------------------------------------------------------------
# D. dispatch_initialize_mission 整合
# ---------------------------------------------------------------------------


class TestInitializeMissionAutoInvoke:
    async def _handshake(self, ds):
        handshake = await ds.dispatch_handshake(dispatch_token="internal-secret", caller_name="Alice")
        return handshake.split("Session token:")[-1].strip()

    @pytest.mark.asyncio
    async def test_auto_invoke_triggers_popen(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
        from memory_server import config_utils

        from ToukaDispatch import server as ds

        config_utils._reset_resource_proxy_cache()
        f = _write_hooks(tmp_path, '{"on_mission_created": {"claude-code": {"command": ["claude", "-p", ""]}}}')
        monkeypatch.setattr(ds, "_HOOKS_PATH", f)
        popen_calls = []
        monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k)) or object())

        session_token = await self._handshake(ds)
        result = await ds.dispatch_initialize_mission(
            session_token=session_token, goal="Test", auto_invoke="claude-code"
        )
        assert "🚀" in result
        assert len(popen_calls) == 1

    @pytest.mark.asyncio
    async def test_no_auto_invoke_no_popen(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
        from memory_server import config_utils

        from ToukaDispatch import server as ds

        config_utils._reset_resource_proxy_cache()
        called = []
        monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: called.append(1))

        session_token = await self._handshake(ds)
        result = await ds.dispatch_initialize_mission(session_token=session_token, goal="Test")
        assert "🚀" not in result
        assert not called

    @pytest.mark.asyncio
    async def test_external_session_auto_invoke_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
        from memory_server import config_utils

        from ToukaDispatch import server as ds

        config_utils._reset_resource_proxy_cache()
        called = []
        monkeypatch.setattr(ds.subprocess, "Popen", lambda *a, **k: called.append(1))

        ds._session_mgr.create_mission_token("mid")
        external = ds._session_mgr.create_external_session("mid", {"name": "Ext"})
        result = await ds.dispatch_initialize_mission(
            session_token=external, goal="hijack", auto_invoke="claude-code"
        )
        assert "Error" in result
        assert not called


# ---------------------------------------------------------------------------
# E. 靜態檢查（AST）
# ---------------------------------------------------------------------------


class TestStaticChecks:
    def test_initialize_mission_has_auto_invoke_param(self):
        tree = ast.parse(_DISPATCH_SERVER.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "dispatch_initialize_mission":
                params = {a.arg for a in node.args.args}
                assert "auto_invoke" in params
                return
        pytest.fail("dispatch_initialize_mission not found")

    def test_no_shell_true_in_server(self):
        src = _DISPATCH_SERVER.read_text(encoding="utf-8")
        assert "shell=True" not in src, "subprocess 絕不可用 shell=True (指令注入防線)"
