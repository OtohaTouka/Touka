"""Hook 服務：載入設定、比對、執行、合併結果、執行紀錄與存檔（ADR-0292）。

解讀照 Claude Code：結束碼 2 擋下（原因取 stderr）；結束碼 0 時讀 JSON；其他結束碼與 hook 自己的
失敗（逾時、連不上、JSON 壞掉）都不擋、但記下來。hook 只能擋不能放行：``allow``、``updatedInput``
一類欄位不採用。同一事件的 hook 同時執行，任一個擋下就擋下。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.models.hooks import HooksConfig
from src.services.hooks import service as service_module
from src.services.hooks.outcome import interpret
from src.services.hooks.paths import HookZones
from src.services.hooks.runners import RunResult
from src.services.hooks.service import RUN_LOG_SIZE, HookService

if TYPE_CHECKING:
    from pathlib import Path

_OK = RunResult(status="ok", duration_ms=1, exit_code=0)


def _ok(stdout: str = "", exit_code: int = 0, stderr: str = "") -> RunResult:
    return RunResult(status="ok", duration_ms=1, exit_code=exit_code, stdout=stdout, stderr=stderr)


class _FakeRunner:
    """記下每次呼叫，回傳指定結果；``gate`` 讓測試確認同時執行。"""

    def __init__(self, result: RunResult | None = None, *, gate: asyncio.Event | None = None):
        self.result = result or _OK
        self.calls: list[tuple[object, bytes]] = []
        self.gate = gate
        self.started = 0

    async def __call__(self, hook, payload: bytes, **_kwargs) -> RunResult:
        self.calls.append((hook, payload))
        self.started += 1
        if self.gate is not None:
            await asyncio.wait_for(self.gate.wait(), timeout=5)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "hooks").mkdir(parents=True)
    (directory / "hooks" / "guard.py").write_text("", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "evil.py").write_text("", encoding="utf-8")
    return directory


def _zones_for(config_dir: Path):
    async def provider() -> HookZones:
        return HookZones(config_dir=config_dir, forbidden=(config_dir.parent / "data",))

    return provider


def _write(config_dir: Path, data: dict) -> None:
    (config_dir / "hooks.json").write_text(json.dumps(data), encoding="utf-8")


def _command(*args: str, name: str | None = None) -> dict:
    hook = {"type": "command", "command": sys.executable, "args": list(args)}
    return {**hook, "name": name} if name else hook


def _service(config_dir: Path, command=None, http=None, zones=None) -> HookService:
    return HookService(
        config_path=lambda: config_dir / "hooks.json",
        zones_provider=zones or _zones_for(config_dir),
        command_runner=command or _FakeRunner(),
        http_runner=http or _FakeRunner(),
    )


def _enabled(event: str, *groups: dict) -> dict:
    return {"enabled": True, "hooks": {event: list(groups)}}


# --- 解讀 -------------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PreToolUse"])
def test_exit_2_blocks_with_stderr_as_reason(event):
    verdict = interpret(event, _ok(exit_code=2, stderr="no secrets please\n", stdout='{"x": 1}'))
    assert verdict.blocked is True
    assert verdict.reason == "no secrets please"


def test_exit_2_after_a_tool_ran_is_feedback_not_a_block():
    verdict = interpret("PostToolUse", _ok(exit_code=2, stderr="lint failed"))
    assert verdict.blocked is False
    assert verdict.context == "lint failed"


def test_stop_is_only_observed():
    verdict = interpret("Stop", _ok(exit_code=2, stderr="keep going"))
    assert (verdict.blocked, verdict.context) == (False, None)
    assert verdict.notes


@pytest.mark.parametrize(
    ("event", "body", "blocked", "ask", "context"),
    [
        ("UserPromptSubmit", {"decision": "block", "reason": "r"}, True, False, None),
        ("UserPromptSubmit", {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": "ctx"}}, False, False, "ctx"),
        ("PreToolUse", {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "r"}}, True, False, None),
        ("PreToolUse", {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                               "permissionDecision": "ask"}}, False, True, None),
        ("PreToolUse", {"decision": "block", "reason": "r"}, True, False, None),
        ("PostToolUse", {"decision": "block", "reason": "fix it"}, False, False, "fix it"),
        ("PostToolUse", {"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": "ctx"}}, False, False, "ctx"),
    ],
)
def test_json_output_fields(event, body, blocked, ask, context):
    verdict = interpret(event, _ok(stdout=json.dumps(body)))
    assert (verdict.blocked, verdict.ask, verdict.context) == (blocked, ask, context)
    assert verdict.failed is False


@pytest.mark.parametrize(
    "hook_output",
    [
        {"permissionDecision": "allow"},
        {"permissionDecision": "defer"},
        {"updatedInput": {"command": "rm -rf /"}},
    ],
)
def test_hooks_can_not_allow_or_rewrite(hook_output):
    body = {"hookSpecificOutput": {"hookEventName": "PreToolUse", **hook_output}}
    verdict = interpret("PreToolUse", _ok(stdout=json.dumps(body)))
    assert (verdict.blocked, verdict.ask) == (False, False)
    assert verdict.notes  # 設定頁看得到「這個欄位不採用」


def test_other_event_name_in_output_is_ignored():
    body = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "permissionDecision": "deny"}}
    verdict = interpret("PreToolUse", _ok(stdout=json.dumps(body)))
    assert verdict.blocked is False
    assert verdict.notes


@pytest.mark.parametrize(
    "result",
    [
        _ok(exit_code=1, stderr="boom"),
        _ok(stdout="{not json"),
        RunResult(status="timeout", duration_ms=1),
        RunResult(status="error", duration_ms=1, detail="ConnectError"),
        RunResult(status="rejected", duration_ms=0, detail="args[0]: agent_writable"),
    ],
)
def test_hook_failures_do_not_block(result):
    verdict = interpret("PreToolUse", result)
    assert (verdict.blocked, verdict.failed) == (False, True)


_DEEP_JSON = '{"a": ' * 100_000 + '1' + '}' * 100_000
_HUGE_NUMBER = '{"n": ' + "9" * 5_000 + "}"


@pytest.mark.parametrize("stdout", [_DEEP_JSON, _HUGE_NUMBER], ids=["deep", "huge_number"])
def test_json_that_python_can_not_load_is_a_hook_failure(stdout):
    verdict = interpret("PreToolUse", _ok(stdout=stdout))
    assert (verdict.blocked, verdict.failed) == (False, True)


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PreToolUse"])
def test_continue_false_blocks_with_the_stop_reason_first(event):
    body = {
        "continue": False, "stopReason": "stop here", "decision": "block", "reason": "other",
        "hookSpecificOutput": {"hookEventName": event, "permissionDecision": "ask"},
    }
    verdict = interpret(event, _ok(stdout=json.dumps(body)))
    assert (verdict.blocked, verdict.ask, verdict.reason) == (True, False, "stop here")


@pytest.mark.parametrize("event", ["PostToolUse", "Stop"])
def test_continue_false_after_the_tool_or_turn_is_only_noted(event):
    verdict = interpret(event, _ok(stdout=json.dumps({"continue": False, "stopReason": "x"})))
    assert (verdict.blocked, verdict.failed) == (False, False)
    assert any("continue" in note for note in verdict.notes)


def test_plain_stdout_is_context_only_for_prompts():
    assert interpret("UserPromptSubmit", _ok(stdout="today is a holiday\n")).context == (
        "today is a holiday"
    )
    assert interpret("PreToolUse", _ok(stdout="hello")).context is None


def test_http_success_reads_the_body_like_stdout():
    result = RunResult(status="ok", duration_ms=1, http_status=200,
                       stdout='{"decision": "block", "reason": "r"}')
    assert interpret("UserPromptSubmit", result).blocked is True
    assert interpret("UserPromptSubmit", RunResult(status="ok", duration_ms=1, http_status=204)) \
        .failed is False


# --- 載入與比對 ---------------------------------------------------------------------------


async def test_disabled_or_missing_config_runs_nothing(config_dir):
    runner = _FakeRunner()
    service = _service(config_dir, command=runner)
    assert service.health().status == "missing"
    assert (await service.dispatch("PreToolUse", {}, match=["x"])).blocked is False
    _write(config_dir, {**_enabled("PreToolUse", {"hooks": [_command()]}), "enabled": False})
    assert service.has_hooks("PreToolUse") is False
    await service.dispatch("PreToolUse", {}, match=["x"])
    assert runner.calls == []
    assert service.runs() == []


async def test_matcher_selects_hooks_for_tool_events(config_dir):
    runner = _FakeRunner()
    _write(config_dir, _enabled("PreToolUse",
                                {"matcher": "terminal_execute", "hooks": [_command("a")]},
                                {"matcher": "mcp__memory__.*", "hooks": [_command("b")]}))
    service = _service(config_dir, command=runner)
    await service.dispatch("PreToolUse", {"tool_name": "vault_read"}, match=["vault_read"])
    assert runner.calls == []
    await service.dispatch("PreToolUse", {}, match=["memory_query", "mcp__memory__query"])
    assert [hook.args for hook, _ in runner.calls] == [["b"]]


async def test_prompt_and_stop_hooks_ignore_the_matcher(config_dir):
    runner = _FakeRunner()
    _write(config_dir, _enabled("UserPromptSubmit", {"matcher": "never", "hooks": [_command()]}))
    await _service(config_dir, command=runner).dispatch("UserPromptSubmit", {"prompt": "hi"})
    assert len(runner.calls) == 1


async def test_payload_has_the_event_name_and_goes_to_the_runner(config_dir):
    runner = _FakeRunner()
    _write(config_dir, _enabled("UserPromptSubmit", {"hooks": [_command()]}))
    await _service(config_dir, command=runner).dispatch("UserPromptSubmit", {"prompt": "hi"})
    (_, payload), = runner.calls
    assert json.loads(payload) == {"hook_event_name": "UserPromptSubmit", "prompt": "hi"}


async def test_a_lone_surrogate_in_the_payload_still_runs_the_hooks(config_dir):
    """模型可以送出落單的 surrogate；編不成 UTF-8 時不能讓整批 hook 都不跑。"""
    runner = _FakeRunner()
    _write(config_dir, _enabled("UserPromptSubmit", {"hooks": [_command()]}))
    service = _service(config_dir, command=runner)
    await service.dispatch("UserPromptSubmit", {"prompt": "a" + chr(0xD800) + "b"})
    (_, payload), = runner.calls
    assert json.loads(payload)["prompt"] == "a?b"
    assert [record.status for record in service.runs()] == ["ok"]


async def test_identical_hooks_run_once_and_disabled_hooks_not_at_all(config_dir):
    runner = _FakeRunner()
    off = {**_command("x"), "enabled": False}
    twice = {"hooks": [_command("a"), _command("a")]}
    _write(config_dir, _enabled("Stop", twice, {"hooks": [off]}))
    await _service(config_dir, command=runner).dispatch("Stop", {})
    assert [hook.args for hook, _ in runner.calls] == [["a"]]


# --- 執行與合併 ---------------------------------------------------------------------------


async def test_hooks_run_at_the_same_time_and_any_block_wins(config_dir):
    gate = asyncio.Event()
    blocker = _FakeRunner(_ok(exit_code=2, stderr="nope"), gate=gate)
    talker = _FakeRunner(_ok(stdout=json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": "ctx"}})), gate=gate)
    _write(config_dir, _enabled("UserPromptSubmit", {"hooks": [
        _command("block"), {"type": "http", "url": "https://hooks.example.test/x"}]}))
    service = _service(config_dir, command=blocker, http=talker)
    task = asyncio.create_task(service.dispatch("UserPromptSubmit", {"prompt": "x"}))
    for _ in range(100):
        if blocker.started and talker.started:
            break
        await asyncio.sleep(0.01)
    assert (blocker.started, talker.started) == (1, 1)
    gate.set()
    decision = await task
    assert decision.blocked is True
    assert decision.reasons == ["nope"]
    assert decision.contexts == ["ctx"]


async def test_runner_crash_is_recorded_and_does_not_block(config_dir):
    _write(config_dir, _enabled("PreToolUse", {"hooks": [_command()]}))
    service = _service(config_dir, command=_FakeRunner(RuntimeError("bug")))
    decision = await service.dispatch("PreToolUse", {}, match=["x"])
    assert decision.blocked is False
    (record,) = service.runs()
    assert record.status == "error"
    assert record.outcome == "failed"


async def test_path_check_runs_again_before_each_command(config_dir):
    runner = _FakeRunner()
    _write(config_dir, _enabled("PreToolUse", {"hooks": [
        _command(str(config_dir.parent / "data" / "evil.py"))]}))
    service = _service(config_dir, command=runner)
    decision = await service.dispatch("PreToolUse", {}, match=["x"])
    assert runner.calls == []
    assert decision.blocked is False
    (record,) = service.runs()
    assert record.status == "rejected"
    assert "outside_hooks_dir" in record.detail


async def test_zone_lookup_failure_means_commands_do_not_run(config_dir):
    async def broken() -> HookZones:
        raise OSError

    runner = _FakeRunner()
    _write(config_dir, _enabled("PreToolUse", {"hooks": [_command()]}))
    service = _service(config_dir, command=runner, zones=broken)
    await service.dispatch("PreToolUse", {}, match=["x"])
    assert runner.calls == []
    assert service.runs()[0].status == "rejected"


async def test_unreadable_result_counts_as_that_hook_failing(config_dir, monkeypatch):
    def broken(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(service_module, "interpret", broken)
    _write(config_dir, _enabled("PreToolUse", {"hooks": [_command(name="guard")]}))
    service = _service(config_dir)
    decision = await service.dispatch("PreToolUse", {}, match=["x"])
    assert decision.blocked is False
    (record,) = service.runs()
    assert (record.hook, record.outcome) == ("guard", "failed")


async def test_run_log_errors_do_not_lose_the_decision(config_dir, monkeypatch):
    _write(config_dir, _enabled("PreToolUse", {"hooks": [_command()]}))
    service = _service(config_dir, command=_FakeRunner(_ok(exit_code=2, stderr="nope")))

    def broken(*_args, **_kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(service, "_record", broken)
    assert (await service.dispatch("PreToolUse", {}, match=["x"])).blocked is True


async def test_unnamed_http_hooks_are_labelled_by_host(config_dir):
    _write(config_dir, _enabled("Stop", {"hooks": [
        {"type": "http", "url": "https://hooks.example.test:8443/x"}]}))
    service = _service(config_dir)
    await service.dispatch("Stop", {})
    assert service.runs()[0].hook == "hooks.example.test"


async def test_deleted_config_keeps_the_last_good_one_until_it_is_back(config_dir):
    _write(config_dir, _enabled("Stop", {"hooks": [_command()]}))
    service = _service(config_dir)
    assert service.has_hooks("Stop") is True
    (config_dir / "hooks.json").unlink()
    assert service.has_hooks("Stop") is True
    assert (service.health().status, service.health().detail) == ("error", "FileNotFoundError")
    _write(config_dir, _enabled("Stop", {"hooks": [_command()]}))
    assert service.health().status == "ok"


async def test_read_errors_keep_the_last_good_one_and_recover(config_dir, monkeypatch):
    _write(config_dir, _enabled("Stop", {"hooks": [_command()]}))
    service = _service(config_dir)
    good = service.health().revision
    read = service._read_raw

    def broken():
        raise PermissionError

    monkeypatch.setattr(service, "_read_raw", broken)
    assert service.has_hooks("Stop") is True
    assert service.health().status == "error"
    monkeypatch.setattr(service, "_read_raw", read)
    assert (service.health().status, service.health().revision) == ("ok", good)


@pytest.mark.parametrize("raw", [_DEEP_JSON, _HUGE_NUMBER], ids=["deep", "huge_number"])
async def test_config_python_can_not_load_keeps_the_last_good_one(config_dir, raw):
    _write(config_dir, _enabled("Stop", {"hooks": [_command()]}))
    service = _service(config_dir)
    assert service.has_hooks("Stop") is True
    (config_dir / "hooks.json").write_text(raw, encoding="utf-8")
    assert service.has_hooks("Stop") is True
    assert service.health().status == "error"


async def test_broken_config_at_runtime_keeps_the_last_good_one(config_dir):
    runner = _FakeRunner()
    _write(config_dir, _enabled("Stop", {"hooks": [_command()]}))
    service = _service(config_dir, command=runner)
    assert service.has_hooks("Stop") is True
    (config_dir / "hooks.json").write_text("{broken", encoding="utf-8")
    assert service.has_hooks("Stop") is True
    assert service.health().status == "error"
    _write(config_dir, {"enabled": True, "hooks": {"SessionStart": []}})
    assert service.has_hooks("Stop") is True
    assert service.health().status == "error"


# --- 執行紀錄 -----------------------------------------------------------------------------


async def test_run_log_records_results_and_hides_details_for_incognito(config_dir):
    runner = _FakeRunner(_ok(exit_code=2, stderr="secret prompt text"))
    _write(config_dir, _enabled("UserPromptSubmit", {"hooks": [_command(name="guard")]}))
    service = _service(config_dir, command=runner)
    await service.dispatch("UserPromptSubmit", {"prompt": "a"})
    await service.dispatch("UserPromptSubmit", {"prompt": "b"}, incognito=True)
    private, normal = service.runs()
    assert (normal.hook, normal.outcome) == ("guard", "blocked")
    assert normal.detail == "secret prompt text"
    assert (private.event, private.status) == ("UserPromptSubmit", "ok")
    assert private.outcome == "blocked"
    assert (private.hook, private.detail, private.exit_code) == ("", None, None)
    assert private.incognito is True


async def test_run_log_keeps_only_the_latest_runs(config_dir):
    _write(config_dir, _enabled("Stop", {"hooks": [_command()]}))
    service = _service(config_dir)
    for _ in range(RUN_LOG_SIZE + 5):
        await service.dispatch("Stop", {})
    assert len(service.runs()) == RUN_LOG_SIZE


# --- 存檔 ---------------------------------------------------------------------------------


def _config(data: dict) -> HooksConfig:
    return HooksConfig.model_validate(data)


async def test_save_writes_the_file_and_checks_the_revision(config_dir):
    service = _service(config_dir)
    assert service.health().revision is None
    first = await service.save(_config(_enabled("Stop", {"hooks": [_command()]})), None)
    assert service.has_hooks("Stop") is True
    assert json.loads((config_dir / "hooks.json").read_text(encoding="utf-8"))["enabled"] is True
    with pytest.raises(ToukaException) as stale:
        await service.save(_config({"enabled": False}), None)
    assert stale.value.code == ErrorCode.HOOK_CONFIG_CONFLICT
    second = await service.save(_config({"enabled": False}), first)
    assert second != first
    assert service.health().revision == second


async def test_a_failed_write_is_a_save_error_and_keeps_the_old_config(config_dir, monkeypatch):
    service = _service(config_dir)
    first = await service.save(_config(_enabled("Stop", {"hooks": [_command()]})), None)

    def disk_full(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(service_module.resource_manager, "write_json", disk_full)
    with pytest.raises(ToukaException) as failed:
        await service.save(_config({"enabled": False}), first)
    assert failed.value.code == ErrorCode.CONFIG_SAVE_FAILED
    assert service.has_hooks("Stop") is True
    assert service.health().revision == first


async def test_a_config_that_cannot_be_read_before_saving_is_a_save_error(config_dir, monkeypatch):
    service = _service(config_dir)
    first = await service.save(_config(_enabled("Stop", {"hooks": [_command()]})), None)
    before = (config_dir / "hooks.json").read_bytes()
    written: list[object] = []

    def locked(*_args: object, **_kwargs: object) -> bytes:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(service_module.resource_manager, "read_file_binary", locked)
    monkeypatch.setattr(service_module.resource_manager, "write_json",
                        lambda *args, **kwargs: written.append(args))
    with pytest.raises(ToukaException) as failed:
        await service.save(_config({"enabled": False}), first)
    assert failed.value.code == ErrorCode.CONFIG_SAVE_FAILED
    assert written == []
    assert (config_dir / "hooks.json").read_bytes() == before
    assert service.has_hooks("Stop") is True


async def test_save_rejects_programs_in_agent_writable_places(config_dir):
    service = _service(config_dir)
    bad = _enabled("PreToolUse", {"hooks": [
        {"type": "command", "command": str(config_dir.parent / "data" / "evil.py")}]})
    with pytest.raises(ToukaException) as rejected:
        await service.save(_config(bad), None)
    assert rejected.value.code == ErrorCode.HOOK_PATH_REJECTED
    assert rejected.value.detail["problems"][0]["reason"] == "agent_writable"
    assert not (config_dir / "hooks.json").exists()


async def test_save_checks_disabled_hooks_too(config_dir):
    off = {"type": "command", "command": str(config_dir.parent / "data" / "evil.py"),
           "enabled": False}
    with pytest.raises(ToukaException):
        await _service(config_dir).save(_config(_enabled("Stop", {"hooks": [off]})), None)


async def test_save_needs_the_path_check(config_dir):
    async def broken() -> HookZones:
        raise OSError

    service = _service(config_dir, zones=broken)
    with pytest.raises(ToukaException) as rejected:
        await service.save(_config(_enabled("Stop", {"hooks": [_command()]})), None)
    assert rejected.value.code == ErrorCode.HOOK_PATH_REJECTED
    http_only = _enabled("Stop", {"hooks": [{"type": "http", "url": "https://example.test/x"}]})
    assert await service.save(_config(http_only), None)


# --- 真的執行一次 -------------------------------------------------------------------------


async def test_real_command_hook_blocks_a_prompt(config_dir):
    script = config_dir / "hooks" / "guard.py"
    script.write_text(
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        "if 'password' in event['prompt']:\n"
        "    sys.stderr.write('do not paste passwords')\n"
        "    sys.exit(2)\n",
        encoding="utf-8",
    )
    _write(config_dir, _enabled("UserPromptSubmit", {"hooks": [
        {"type": "command", "command": sys.executable, "args": [str(script)]}]}))
    service = HookService(
        config_path=lambda: config_dir / "hooks.json", zones_provider=_zones_for(config_dir)
    )
    blocked = await service.dispatch("UserPromptSubmit", {"prompt": "my password is x"})
    assert (blocked.blocked, blocked.reasons) == (True, ["do not paste passwords"])
    assert (await service.dispatch("UserPromptSubmit", {"prompt": "hello"})).blocked is False
