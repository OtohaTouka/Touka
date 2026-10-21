"""H3：演化沙盒用 venv Python 的絕對路徑呼叫 pip 與本機驗證（iso-c1 U2）.

修補前：pip 安裝與本機驗證把相對路徑 ``Scripts/python.exe``（POSIX ``bin/python``）當執行檔，
Windows 找執行檔不看子程序的 ``cwd``；而且 venv 建在沙盒的 ``.venv`` 底下，相對路徑本來就指錯
位置。修補後兩個子程序拿到的是 ``<沙盒>/.venv/...`` 的絕對路徑，也就是 venv 建立步驟產生的那支。
部署寫進 ``server_config.json`` 的 ``command`` 同理（原本是 ``<沙盒>/Scripts/python.exe``，少了
``.venv``，部署後的伺服器指向不存在的直譯器）。

子程序一律換成記錄器，不建 venv、不連網；沙盒根目錄指到 pytest 的 tmp；部署卡與設定寫入
換成替身，不寫真的設定檔、不熱掛載。
"""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import types
from src.main import app
from src.tools.builtin import evolution

_VENV_PYTHON = {True: Path(".venv/Scripts/python.exe"), False: Path(".venv/bin/python")}


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def _run(_skill_id: str, cmd_list: list[str], **kwargs: Any) -> SimpleNamespace:
        seen.append({"cmd": list(cmd_list), "cwd": kwargs.get("cwd")})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    guard = evolution.EvolutionPathGuard
    monkeypatch.setattr(guard, "get_tool_root", lambda _skill_id: tmp_path)
    monkeypatch.setattr(guard, "tool_file_exists", lambda *_a: False)
    monkeypatch.setattr(guard, "write_tool_file", lambda *_a: None)
    monkeypatch.setattr(guard, "delete_tool_file", lambda *_a: True)
    monkeypatch.setattr(guard, "run_sandbox_command", _run)
    return seen


@pytest.fixture
def verification(monkeypatch):
    session = SimpleNamespace(call_tool=AsyncMock(return_value=types.CallToolResult(content=[])))
    connect = AsyncMock(return_value=(SimpleNamespace(session=session), AsyncExitStack()))
    monkeypatch.setattr(evolution, "connect_mcp_server", connect)

    @asynccontextmanager
    async def windows(config, timeout, _tail):
        client, stack = await connect(config, read_timeout_seconds=timeout)
        async with stack:
            yield client

    monkeypatch.setattr(evolution, "_windows_client", windows)
    return connect, session.call_tool


@pytest.mark.parametrize("windows", [True, False])
async def test_pip_install_uses_the_absolute_venv_python(monkeypatch, tmp_path, commands, windows):
    monkeypatch.setattr(evolution.EvolutionPathGuard, "is_windows", lambda: windows)
    out = await evolution.write_mcp_implementation("skill_h3", "pass", ["example-package"])
    assert out.startswith("Development Success")
    venv_create, pip_install = (entry["cmd"] for entry in commands)
    assert venv_create == [sys.executable, "-m", "venv", ".venv"]
    assert Path(pip_install[0]) == tmp_path / _VENV_PYTHON[windows]
    assert Path(pip_install[0]).is_absolute()
    assert pip_install[1:] == ["-m", "pip", "install", "-r", "requirements.txt"]


@pytest.mark.parametrize("windows", [True, False])
async def test_local_verification_uses_the_absolute_venv_python(
    monkeypatch, tmp_path, commands, verification, windows
):
    monkeypatch.setattr(evolution.EvolutionPathGuard, "is_windows", lambda: windows)
    outcome = await evolution._verify_mcp("skill_h3", "probe", {})
    assert outcome.passed
    connect, call_tool = verification
    connect.assert_awaited_once()
    config = connect.call_args.args[0]
    verify = [config.command, *config.args]
    assert Path(verify[0]) == tmp_path / _VENV_PYTHON[windows]
    assert Path(verify[0]).is_absolute()
    assert verify[1:] == [str(tmp_path / "test_wrapper.py")]
    if not windows:
        assert connect.call_args.kwargs["isolated"].launch.argv == tuple(verify)
    call_tool.assert_awaited_once_with("probe", {})
    assert commands == []


@pytest.mark.parametrize("windows", [True, False])
async def test_deploy_command_is_the_python_the_build_step_created(
    monkeypatch, tmp_path, commands, verification, windows
):
    guard = evolution.EvolutionPathGuard
    monkeypatch.setattr(guard, "is_windows", lambda: windows)
    await evolution.write_mcp_implementation("skill_h3", "pass", ["example-package"])
    venv_create, pip_install = (entry["cmd"] for entry in commands)
    built_python = tmp_path / venv_create[-1] / _VENV_PYTHON[windows].relative_to(".venv")
    outcome = await evolution._verify_mcp("skill_h3", "probe", {})
    assert outcome.passed
    connect, _ = verification
    assert connect.call_args.args[0].command == pip_install[0]

    saved: dict[str, Any] = {}
    config = MagicMock()
    config.read_config_json.return_value = {}
    config.save_config_json.side_effect = lambda _name, data: saved.update(data)
    monkeypatch.setattr(evolution, "config_utils", config)
    monkeypatch.setattr(evolution, "request_approval", AsyncMock(return_value=(True, {})))
    monkeypatch.setattr(guard, "tool_file_exists", lambda *_a: True)
    monkeypatch.setattr(
        guard, "read_tool_file", lambda _s, name: '{"name": "H3"}' if name == "spec.json" else ""
    )
    (tmp_path / "server.py").write_bytes(b"pass")
    (tmp_path / "requirements.txt").write_bytes(b"")
    monkeypatch.setattr(app.state, "context", {}, raising=False)  # 不熱掛載

    out = await evolution.request_mcp_creation("skill_h3", _interactive=True)

    assert out.startswith("Evolution Success")
    entry = saved["mcpServers"]["skill_h3"]
    assert Path(entry["command"]) == built_python
    assert entry["command"] == pip_install[0]  # 與建置、驗證用的是同一支
    assert entry["args"] == [str(tmp_path / "server.py")]
