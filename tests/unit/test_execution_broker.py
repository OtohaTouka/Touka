"""ExecutionBroker 純轉傳契約（ADR-0239 §2，純重構、零行為變更）。

鎖定三件事：
- ``new_process_group_kwargs()`` 於「呼叫當下」依平台決定群組 kwargs（POSIX 絕不碰
  ``subprocess.CREATE_NEW_PROCESS_GROUP``）；
- ``spawn_exec``／``run_sync`` 原樣轉傳 argv 與 kwargs，不注入任何預設值、不檢視回傳物件；
- 一律經模組屬性呼叫 ``asyncio.create_subprocess_exec``／``subprocess.run``，故既有測試對
  全域的 patch 仍生效（零測試修改的前提）。
全程以替身取代真實子程序（沙盒、零外部副作用）。
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
from pathlib import Path

import pytest
from src.services import execution_broker

_WIN_FLAG = 512  # 替身值：POSIX 上 subprocess 無 CREATE_NEW_PROCESS_GROUP，需自行補


class _Untouchable:
    """回傳物件替身：任何屬性存取都視為違約（broker 不得檢視子程序物件）。"""

    def __getattribute__(self, name: str) -> object:
        msg = f"broker must not touch the returned process (accessed {name!r})"
        raise AssertionError(msg)


def _force_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_broker.sys, "platform", "win32")
    monkeypatch.setattr(
        execution_broker.subprocess, "CREATE_NEW_PROCESS_GROUP", _WIN_FLAG, raising=False
    )


def _force_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_broker.sys, "platform", "linux")
    # POSIX 分支絕不可讀取此屬性：移除後若被讀取即 AttributeError。
    monkeypatch.delattr(execution_broker.subprocess, "CREATE_NEW_PROCESS_GROUP", raising=False)


def _record_async_exec(monkeypatch: pytest.MonkeyPatch, result: object) -> list[tuple]:
    calls: list[tuple] = []

    async def _fake_exec(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return result

    # 經全域 asyncio 模組屬性 patch（同既有 terminal／dispatch_supervisor 測試的 seam）。
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return calls


def _record_sync_run(monkeypatch: pytest.MonkeyPatch, result: object) -> list[tuple]:
    calls: list[tuple] = []

    def _fake_run(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return result

    # 經全域 subprocess 模組屬性 patch（同既有 evolution_security／system_backup 測試的 seam）。
    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


# ---------------------------------------------------------------------------
# new_process_group_kwargs
# ---------------------------------------------------------------------------


def test_group_kwargs_windows(monkeypatch):
    _force_windows(monkeypatch)
    assert execution_broker.new_process_group_kwargs() == {"creationflags": _WIN_FLAG}


def test_group_kwargs_posix_never_touches_windows_flag(monkeypatch):
    _force_posix(monkeypatch)
    assert execution_broker.new_process_group_kwargs() == {"start_new_session": True}


def test_group_kwargs_evaluated_at_call_time(monkeypatch):
    """平台於每次呼叫時才判定（非 import 時快取）。"""
    _force_posix(monkeypatch)
    assert execution_broker.new_process_group_kwargs() == {"start_new_session": True}
    _force_windows(monkeypatch)
    assert execution_broker.new_process_group_kwargs() == {"creationflags": _WIN_FLAG}


def test_group_kwargs_returns_fresh_dict(monkeypatch):
    _force_posix(monkeypatch)
    first = execution_broker.new_process_group_kwargs()
    first["mutated"] = True
    assert execution_broker.new_process_group_kwargs() == {"start_new_session": True}


# ---------------------------------------------------------------------------
# spawn_exec
# ---------------------------------------------------------------------------


async def test_spawn_exec_passes_argv_and_kwargs_unchanged(monkeypatch):
    sentinel = _Untouchable()
    calls = _record_async_exec(monkeypatch, sentinel)
    env = {"A": "1"}

    result = await execution_broker.spawn_exec(
        "prog",
        "--flag",
        "value",
        cwd="somewhere",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    assert result is sentinel
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("prog", "--flag", "value")
    assert kwargs == {
        "cwd": "somewhere",
        "env": env,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
    }
    assert kwargs["env"] is env


async def test_spawn_exec_injects_no_defaults(monkeypatch):
    calls = _record_async_exec(monkeypatch, object())
    await execution_broker.spawn_exec("prog")
    assert calls == [(("prog",), {})]


async def test_spawn_exec_new_process_group_posix(monkeypatch):
    _force_posix(monkeypatch)
    calls = _record_async_exec(monkeypatch, object())
    await execution_broker.spawn_exec("prog", "x", new_process_group=True, cwd="d")
    assert calls == [(("prog", "x"), {"cwd": "d", "start_new_session": True})]


async def test_spawn_exec_new_process_group_windows(monkeypatch):
    _force_windows(monkeypatch)
    calls = _record_async_exec(monkeypatch, object())
    await execution_broker.spawn_exec("prog", new_process_group=True, env={})
    assert calls == [(("prog",), {"env": {}, "creationflags": _WIN_FLAG})]


async def test_spawn_exec_without_group_never_evaluates_platform(monkeypatch):
    """new_process_group=False 時不加任何群組 kwargs（review evidence 的既有行為）。"""
    monkeypatch.setattr(
        execution_broker,
        "new_process_group_kwargs",
        lambda: pytest.fail("group kwargs must not be computed"),
    )
    calls = _record_async_exec(monkeypatch, object())
    await execution_broker.spawn_exec("prog", cwd="d")
    assert calls == [(("prog",), {"cwd": "d"})]


async def test_spawn_exec_propagates_spawn_errors(monkeypatch):
    async def _missing(*_a: object, **_k: object) -> object:
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _missing)
    with pytest.raises(FileNotFoundError):
        await execution_broker.spawn_exec("missing-program")


# ---------------------------------------------------------------------------
# run_sync
# ---------------------------------------------------------------------------


def test_run_sync_passes_argv_and_kwargs_unchanged(monkeypatch):
    sentinel = _Untouchable()
    calls = _record_sync_run(monkeypatch, sentinel)
    argv = ["prog", "backup", "--no-docker"]

    result = execution_broker.run_sync(argv, capture_output=True, text=True, check=True)

    assert result is sentinel
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert len(args) == 1
    assert args[0] is argv
    assert kwargs == {"capture_output": True, "text": True, "check": True}


def test_run_sync_injects_no_defaults(monkeypatch):
    calls = _record_sync_run(monkeypatch, object())
    execution_broker.run_sync(["prog"])
    assert calls == [((["prog"],), {})]


def test_run_sync_propagates_errors(monkeypatch):
    def _boom(*_a: object, **_k: object) -> object:
        raise subprocess.CalledProcessError(1, "prog", stderr="boom")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(subprocess.CalledProcessError):
        execution_broker.run_sync(["prog"], check=True)


# ---------------------------------------------------------------------------
# 葉節點模組：只依賴標準庫
# ---------------------------------------------------------------------------


def test_broker_is_stdlib_leaf_module():
    source = Path(execution_broker.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "broker 不得使用相對匯入"
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "asyncio", "subprocess", "sys", "typing"}
