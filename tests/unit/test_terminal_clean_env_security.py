"""TDD — terminal must not leak Touka secrets to child shells.

`bash()` launched subprocess.run WITHOUT env=, so the PowerShell / bash / wsl
children inherited the full parent environment, including TOUKA_AUTH_SECRET,
DISPATCH_TOKEN, HEARTBEAT_TOKEN and provider API keys — an agent running
`echo $TOUKA_AUTH_SECRET` could read them all. The fix passes
`EvolutionPathGuard.get_clean_env()` (the sandbox SSOT) to every child process:
secret-named vars are stripped, functional vars (PATH, ...) are kept.
"""

from __future__ import annotations

import pytest
from src.models.identity import AgentIdentity
from src.services.evolution_security import EvolutionPathGuard
from src.tools import terminal
from src.tools.terminal import bash

SECRET_PATTERNS = ("SECRET", "TOKEN", "KEY", "AUTH", "PASSWORD", "CREDENTIAL")


class _FakeStdout:
    async def readline(self):
        return b""  # 立即 EOF（本測試只關心 env，不關心輸出）


class _FakeProc:
    """最小可用的 async subprocess 替身（取代真實 create_subprocess_exec）。"""

    returncode = 0

    def __init__(self):
        self.stdout = _FakeStdout()

    async def wait(self):
        return 0

    def kill(self):
        pass


@pytest.mark.asyncio
async def test_bash_subprocess_uses_clean_env(monkeypatch):
    captured = {}

    async def _fake_exec(*_args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    # Force the strategy gate to ALLOW and capture the (async) subprocess kwargs.
    monkeypatch.setattr(terminal.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(terminal, "_classify_command_strategy", lambda *a, **k: "ALLOW")

    identity = AgentIdentity.for_role(agent_id="agentA", role="Owner", capabilities=[])
    await bash("echo hi", identity, env="powershell")

    env = captured.get("env")
    assert env is not None, "child shell must be launched with an explicit clean env"
    # functional environment preserved
    assert any(k.upper() == "PATH" for k in env), "PATH must survive sanitisation"
    # no secret-named variable leaks to the child
    leaked = [k for k in env if any(p in k.upper() for p in SECRET_PATTERNS)]
    assert not leaked, f"secret-named env vars leaked to child shell: {leaked}"


def test_get_clean_env_strips_secret_named_vars():
    """The reused SSOT clean-env contains no secret-named variables."""
    env = EvolutionPathGuard.get_clean_env()
    assert not [k for k in env if any(p in k.upper() for p in SECRET_PATTERNS)]
