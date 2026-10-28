"""Hook 的兩種執行方式（ADR-0292）。

command：
- 不經 shell：程式＋參數清單原樣交給作業系統，``|``、``;`` 只是文字。
- 事件內容只從標準輸入給，不放進參數。
- 子程序環境只有白名單（PATH、系統目錄一類），Touka 與廠商金鑰一律不帶。
- 逾時就整棵行程樹結束；stdout／stderr 各有上限，超過的部分丟掉、標成截斷。

http：
- POST 事件內容；不跟隨轉址（3xx 算失敗）、非 2xx 算失敗、回應有大小上限與逾時。
- 標頭照字面送出，不替換環境變數。
"""

from __future__ import annotations

import json
import sys

import httpx
import pytest
from src.models.hooks import CommandHook, HttpHook
from src.services.hooks import runners
from src.services.hooks.runners import MAX_HOOK_OUTPUT_BYTES, run_command, run_http

_ECHO = (
    "import json, os, sys\n"
    "data = sys.stdin.read()\n"
    "print(json.dumps({'stdin': data, 'argv': sys.argv[1:], 'env': sorted(os.environ)}))\n"
)


def _python(code: str, *args: str, timeout: int = 20) -> CommandHook:
    return CommandHook(
        type="command", command=sys.executable, args=["-c", code, *args], timeout=timeout
    )


async def test_payload_goes_to_stdin_only_and_args_are_not_shell(tmp_path):
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "x"}).encode()
    hook = _python(_ECHO, "a|b; echo pwned", "$(whoami)")
    result = await run_command(hook, payload, cwd=tmp_path)
    assert result.status == "ok"
    assert result.exit_code == 0
    seen = json.loads(result.stdout)
    assert json.loads(seen["stdin"]) == {"hook_event_name": "PreToolUse", "tool_name": "x"}
    assert seen["argv"] == ["a|b; echo pwned", "$(whoami)"]


async def test_child_environment_has_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TOUKA_AUTH_SECRET", "secret-for-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DISPATCH_TOKEN", "dispatch-test")
    result = await run_command(_python(_ECHO), b"{}", cwd=tmp_path)
    names = {name.upper() for name in json.loads(result.stdout)["env"]}
    assert {"TOUKA_AUTH_SECRET", "OPENAI_API_KEY", "DISPATCH_TOKEN"}.isdisjoint(names)
    assert "PATH" in names


async def test_exit_code_and_stderr_are_reported(tmp_path):
    code = "import sys\nsys.stderr.write('blocked: no rm\\n')\nsys.exit(2)\n"
    result = await run_command(_python(code), b"{}", cwd=tmp_path)
    assert (result.status, result.exit_code) == ("ok", 2)
    assert result.stderr.strip() == "blocked: no rm"


async def test_timeout_ends_the_process(tmp_path):
    code = "import time\ntime.sleep(30)\n"
    result = await run_command(_python(code, timeout=1), b"{}", cwd=tmp_path)
    assert result.status == "timeout"
    assert result.duration_ms < 15_000


async def test_output_beyond_the_cap_is_dropped(tmp_path):
    code = f"import sys\nsys.stdout.write('x' * {MAX_HOOK_OUTPUT_BYTES * 3})\n"
    result = await run_command(_python(code), b"{}", cwd=tmp_path)
    assert result.status == "ok"
    assert result.truncated is True
    assert len(result.stdout) == MAX_HOOK_OUTPUT_BYTES


async def test_program_that_cannot_start_is_an_error(tmp_path):
    hook = CommandHook(type="command", command=str(tmp_path / "missing.exe"))
    result = await run_command(hook, b"{}", cwd=tmp_path)
    assert result.status == "error"
    assert result.exit_code is None


def _http(**kw) -> HttpHook:
    return HttpHook(type="http", url="https://hooks.example.test/run", timeout=5, **kw)


async def test_http_posts_payload_with_literal_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"decision": "block", "reason": "nope"})

    hook = _http(headers={"Authorization": "Bearer literal-value"})
    result = await run_http(hook, b'{"a": 1}', transport=httpx.MockTransport(handler))
    assert result.status == "ok"
    assert json.loads(result.stdout) == {"decision": "block", "reason": "nope"}
    (request,) = seen
    assert request.method == "POST"
    assert request.content == b'{"a": 1}'
    assert request.headers["authorization"] == "Bearer literal-value"
    assert request.headers["content-type"] == "application/json"


async def test_http_does_not_follow_redirects():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:9/steal"})

    result = await run_http(_http(), b"{}", transport=httpx.MockTransport(handler))
    assert result.status == "error"
    assert seen == ["https://hooks.example.test/run"]


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_http_non_2xx_is_an_error(status):
    transport = httpx.MockTransport(lambda _r: httpx.Response(status, text="bad"))
    result = await run_http(_http(), b"{}", transport=transport)
    assert result.status == "error"


async def test_http_body_beyond_the_cap_is_dropped():
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, content=b"y" * (MAX_HOOK_OUTPUT_BYTES * 2))
    )
    result = await run_http(_http(), b"{}", transport=transport)
    assert result.truncated is True
    assert len(result.stdout) == MAX_HOOK_OUTPUT_BYTES


async def test_http_timeout_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    result = await run_http(_http(), b"{}", transport=httpx.MockTransport(handler))
    assert result.status == "timeout"


def test_http_client_ignores_environment_credentials():
    """``trust_env=False``：不讀 .netrc 與代理設定，hook 網址拿不到其他地方的帳密。"""
    assert runners.HTTP_CLIENT_OPTIONS == {"follow_redirects": False, "trust_env": False}
