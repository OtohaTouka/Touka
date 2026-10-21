"""人工 live probe 的 Gemini 單次請求與 --env-file（model-p0 P-g）。

全部 mock：httpx 傳輸被替換（tests/helpers/stream_transport.py）、金鑰為 sandbox 空值、不傳核准旗標
給任何子程序。「只試 1 次」只在 probe 行程內生效；產品 GeminiAdapter 不受影響。3b-U3 起產品轉換器
依請求帶輸出上限，主迴圈走串流。
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from src.handlers import react_core
from src.handlers.adapters import gemini_adapter
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.stream_timeout import liveness_event_hooks
from src.models.identity import AgentIdentity

from tests.helpers.stream_transport import FakeUpstream, Reply, gemini_stream_reply, status_reply

ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = ROOT / "tests" / "smoke" / "manual" / "live_chat_probe.py"
probe = importlib.import_module("tests.smoke.manual.live_chat_probe")
policy = importlib.import_module("tests.live_api_policy")


async def test_product_gemini_adapter_keeps_retries_and_sends_only_output_limit():
    assert react_core.MAX_SEND_ATTEMPTS == 3  # 3b-U5a：主迴圈的共用重試開關預設 3 次
    assert GeminiAdapter.send_round is probe._ORIGINAL_GEMINI_SEND_ROUND
    request = SimpleNamespace(
        model="gemini-2.5-flash", max_tokens=64, tools=None, pulse_slots=None,
        safety_settings=None, messages=[SimpleNamespace(role="user", content="hi")],
    )
    adapter = GeminiAdapter()
    state = await adapter.prepare_request(request, {"incognito": True})
    await adapter.close(state)
    assert state.payload["generationConfig"] == {"maxOutputTokens": 64}  # 3b-U3：只帶輸出上限


@pytest.mark.parametrize("outcome", ["ok", "503", "connect_error"])
async def test_probe_gemini_sends_exactly_one_capped_request(monkeypatch, outcome):
    for name in ("send_round", "prepare_request"):  # 先快照，teardown 還原產品類別
        monkeypatch.setattr(GeminiAdapter, name, getattr(GeminiAdapter, name))
    monkeypatch.setattr(react_core, "MAX_SEND_ATTEMPTS", react_core.MAX_SEND_ATTEMPTS)
    monkeypatch.setattr(react_core, "_retry_sleep", AsyncMock())
    monkeypatch.setattr(probe, "_PROVIDER_REQUEST_BUDGET", None)
    monkeypatch.setattr(gemini_adapter, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    ok = {"candidates": [{"content": {"parts": [{"text": "LIVE_PROBE_OK"}]},
                          "finishReason": "STOP"}]}
    reply = {"ok": gemini_stream_reply(ok, "one"), "503": status_reply(503, "busy"),
             "connect_error": Reply(error=httpx.ConnectError("synthetic"))}[outcome]
    upstream = FakeUpstream([reply, gemini_stream_reply(ok, "one")]).install(monkeypatch)
    scope = probe._live_chat_scope(model="gemini-2.5-flash", max_tokens=64, provider="gemini")
    run = probe.run_live_chat_probe(
        model="gemini-2.5-flash", max_tokens=64, provider="gemini",
        approval=policy.issue_live_api_approval(approved=True, scope=scope),
    )
    if outcome == "ok":
        assert (await run)["choices"]
    else:
        with pytest.raises(RuntimeError, match="error response"):
            await run
    sent = upstream.sent
    assert len(sent) == 1
    assert probe._PROVIDER_REQUEST_BUDGET.used_requests == 1  # 單次預算確實包住 send_round
    assert sent[0]["generationConfig"] == {"maxOutputTokens": 64}
    assert react_core.MAX_SEND_ATTEMPTS == 1  # 3b-U5a：probe 改用主迴圈的共用重試開關
    assert "cachedContent" not in sent[0]
    assert str(upstream.requests[0].url).endswith(":streamGenerateContent?alt=sse")


_BUSY = "Gemini busy (503), retrying... (attempt {})"


@pytest.mark.parametrize(("retries", "sends", "statuses"), [
    (1, 1, []), (3, 3, [_BUSY.format(1), _BUSY.format(2)])])
async def test_main_loop_reads_the_shared_retry_switch_on_503(monkeypatch, retries, sends,
                                                               statuses):
    """3b-U5a：503 的重試在主迴圈，次數照 react_core.MAX_SEND_ATTEMPTS（probe 設 1）。"""
    monkeypatch.setattr(react_core, "MAX_SEND_ATTEMPTS", retries)
    monkeypatch.setattr(react_core, "_retry_sleep", AsyncMock())
    monkeypatch.setattr(react_core, "accumulate_token_usage", lambda *a, **k: None)
    upstream = FakeUpstream([status_reply(503, "busy") for _ in range(4)]).install(monkeypatch)
    request = SimpleNamespace(
        model="gemini-2.5-flash", max_tokens=None, tools=None, pulse_slots=None,
        safety_settings=None, messages=[SimpleNamespace(role="user", content="hi")],
    )
    ctx = {"incognito": True, "gemini_api_key": "k", "request_id": "req-probe-retry",
           "agent_id": "Sumi", "identity": AgentIdentity.for_role("Sumi", "Owner")}
    chunks = [c async for c in react_core.run_react_loop(GeminiAdapter(stream=True), request, ctx)]
    assert len(upstream.requests) == sends
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    assert [e["status"] for e in events if "status" in e] == statuses  # R-3b ③ L3
    assert "API Error (503)\\n\\n`busy`" in "".join(chunks)
    upstream.assert_all_closed()


def test_cli_accepts_gemini_route_but_still_requires_approval(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")  # 避免 main 改包 pytest 的 stdout
    with pytest.raises(policy.LiveApiApprovalRequired):
        probe.main(["--provider", "gemini", "--model", "gemini-2.5-flash"])


def test_env_file_must_be_an_existing_regular_file_and_is_bound_to_scope(tmp_path):
    fake_env = tmp_path / "fake.env"
    fake_env.write_text("GEMINI_API_KEY=\n", encoding="utf-8")
    assert probe._resolve_env_file([]) == probe.DEFAULT_ENV_FILE
    assert probe._resolve_env_file(["--env-file", str(fake_env)]) == fake_env.resolve()
    for bad in (tmp_path / "missing.env", tmp_path):
        with pytest.raises(SystemExit, match="env-file"):
            probe._resolve_env_file(["--env-file", str(bad)])

    def scope(env_file: Path) -> object:
        return probe._live_chat_scope(model="gpt-test", max_tokens=64, provider="openai",
                                      env_file=env_file)

    assert f"env_file={fake_env.resolve()}" in policy.format_live_api_scope(scope(fake_env))
    approval = policy.issue_live_api_approval(approved=True, scope=scope(probe.DEFAULT_ENV_FILE))
    with pytest.raises(policy.LiveApiApprovalScopeMismatch):
        policy.validate_live_api_approval(approval, expected_scope=scope(fake_env))


def test_env_file_check_precedes_dotenv_and_provider_imports():
    source = PROBE_PATH.read_text(encoding="utf-8")
    check = source.index("_resolve_env_file(sys.argv[1:])")
    assert source.index("preflight_live_api_cli(sys.argv") < check
    assert check < source.index("from dotenv import load_dotenv")
    assert check < source.index("from anthropic import")


def test_cli_without_approval_imports_neither_dotenv_nor_providers(tmp_path):
    fake_env = tmp_path / "fake.env"
    fake_env.write_text("GEMINI_API_KEY=\n", encoding="utf-8")
    argv = ["x", "--provider", "gemini", "--model", "gemini-2.5-flash",
            "--env-file", str(fake_env)]
    code = (
        "import json, runpy, sys\n"
        f"sys.argv, status = {argv!r}, 'no-exit'\n"
        "try:\n"
        f"    runpy.run_path({str(PROBE_PATH)!r}, run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    status = str(exc.code)\n"
        "mods = [m for m in ('dotenv', 'anthropic', 'src') if m in sys.modules]\n"
        "print(json.dumps([status, mods]))\n"
    )
    result = subprocess.run(  # noqa: S603 - 固定直譯器＋固定程式碼，無核准旗標
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        cwd=tmp_path, check=False,
    )
    status, imported = json.loads(result.stdout.strip().splitlines()[-1])
    assert "HUMAN APPROVAL REQUIRED" in status
    assert imported == []


class _Stop(Exception):
    """在 dispatch 前停下：這個測試只看 probe 建出來的 Claude client。"""


async def test_probe_claude_client_times_idle_bytes_like_the_main_loop(monkeypatch):
    """R-3b ② L7：probe 的 Claude client 也掛 liveness_event_hooks()，閒置計時和產品同一條路徑。"""
    for name in ("send_round", "prepare_request"):  # 先快照，teardown 還原產品類別
        monkeypatch.setattr(ClaudeAdapter, name, getattr(ClaudeAdapter, name))
    monkeypatch.setattr(react_core, "MAX_SEND_ATTEMPTS", react_core.MAX_SEND_ATTEMPTS)
    monkeypatch.setattr(probe, "_PROVIDER_REQUEST_BUDGET", None)
    built: list[dict] = []
    real = probe.DefaultAsyncHttpxClient
    monkeypatch.setattr(probe, "DefaultAsyncHttpxClient",
                        lambda **kwargs: built.append(kwargs) or real(**kwargs))

    async def stop(*_args, **_kwargs):
        raise _Stop

    monkeypatch.setattr(probe, "dispatch_request", stop)
    scope = probe._live_chat_scope(model="claude-sonnet-4-5", max_tokens=64,
                                   provider="anthropic")
    with pytest.raises(_Stop):
        await probe.run_live_chat_probe(
            model="claude-sonnet-4-5", max_tokens=64, provider="anthropic",
            approval=policy.issue_live_api_approval(approved=True, scope=scope))
    assert built == [{"follow_redirects": False, "event_hooks": liveness_event_hooks()}]
