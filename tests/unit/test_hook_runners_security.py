"""Hook 的兩種執行方式（ADR-0292）。

command：
- 不經 shell：程式＋參數清單原樣交給作業系統，``|``、``;`` 只是文字。
- 事件內容只從標準輸入給，不放進參數。
- 子程序環境只有白名單（PATH、系統目錄一類），Touka 與廠商金鑰一律不帶。
- 逾時就整棵行程樹結束；stdout／stderr 各有上限，超過的部分丟掉、標成截斷。

http：
- POST 事件內容；不跟隨轉址（3xx 算失敗）、非 2xx 算失敗。
- 逾時是整次請求的上限（伺服器一點一點送也會到時）；回應讀到上限就停止讀取。
- 標頭照字面送出，不替換環境變數。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import threading
from itertools import count

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
    """逾時要真的結束程式：沒結束的話它 3 秒後會留下記號檔。"""
    marker = tmp_path / "still_running"
    code = "import pathlib, sys, time\ntime.sleep(3)\npathlib.Path(sys.argv[1]).write_text('x')\n"
    result = await run_command(_python(code, str(marker), timeout=1), b"{}", cwd=tmp_path)
    assert result.status == "timeout"
    assert result.duration_ms < 3_000
    await asyncio.sleep(3.5)
    assert not marker.exists()


@pytest.mark.parametrize("ending", ["timeout", "cancelled"])
async def test_ending_the_process_does_not_block_other_requests(tmp_path, monkeypatch, ending):
    """Windows 的 taskkill 是同步的：結束程式要在執行緒裡做，不能卡住事件迴圈。"""
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real_kill = runners.terminate_process_tree

    def kill(proc) -> None:
        threads.append(threading.get_ident())
        real_kill(proc)

    monkeypatch.setattr(runners, "terminate_process_tree", kill)
    hook = _python("import time\ntime.sleep(30)\n", timeout=1 if ending == "timeout" else 30)
    if ending == "timeout":
        assert (await run_command(hook, b"{}", cwd=tmp_path)).status == "timeout"
    else:
        task = asyncio.create_task(run_command(hook, b"{}", cwd=tmp_path))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert threads and loop_thread not in threads


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
    return HttpHook(**{"type": "http", "url": "https://hooks.example.test/run", "timeout": 5, **kw})


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


class _WatchedBody(httpx.AsyncByteStream):
    """回應內容：被讀了就記下來。"""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.was_read = False

    async def __aiter__(self):
        self.was_read = True
        yield self.content


@pytest.mark.parametrize("status", [400, 500, 503])
@pytest.mark.parametrize("encoding", [None, "gzip"])
async def test_http_non_2xx_is_an_error(status, encoding):
    """失敗的回應照 HTTP 狀態說明，有壓縮也一樣（不說成壓縮），也不讀內容。"""
    headers = {"Content-Encoding": encoding} if encoding else {}
    body = _WatchedBody(gzip.compress(b"bad") if encoding else b"bad")
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(status, stream=body, headers=headers)
    )
    result = await run_http(_http(), b"{}", transport=transport)
    assert (result.status, result.http_status) == ("error", status)
    assert result.detail == f"HTTP {status}"
    assert result.stdout == ""
    assert body.was_read is False


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


class _EndlessBody(httpx.AsyncByteStream):
    """一直送的回應：``delay`` 秒送一塊，記下送了幾塊。"""

    def __init__(self, chunk: bytes, delay: float = 0.0) -> None:
        self.chunk = chunk
        self.delay = delay
        self.sent = 0

    async def __aiter__(self):
        for self.sent in count(1):
            await asyncio.sleep(self.delay)
            yield self.chunk


async def test_http_timeout_covers_a_server_that_keeps_trickling():
    body = _EndlessBody(b"x", delay=0.05)
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, stream=body))
    result = await asyncio.wait_for(
        run_http(_http(timeout=1), b"{}", transport=transport), timeout=10
    )
    assert result.status == "timeout"


async def test_http_stops_reading_at_the_cap():
    body = _EndlessBody(b"y" * 16_384)
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, stream=body))
    result = await asyncio.wait_for(run_http(_http(), b"{}", transport=transport), timeout=10)
    assert (result.status, result.truncated) == ("ok", True)
    assert body.sent <= MAX_HOOK_OUTPUT_BYTES // 16_384 + 1


def _gzip_bomb(layers: int) -> tuple[bytes, int]:
    """壓縮 ``layers`` 層的一大塊零；回傳送出的位元組與解開後的大小。"""
    size = MAX_HOOK_OUTPUT_BYTES * 64
    data = b"\x00" * size
    for _ in range(layers):
        data = gzip.compress(data)
    return data, size


async def test_http_asks_for_an_uncompressed_reply():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    await run_http(_http(), b"{}", transport=httpx.MockTransport(handler))
    assert seen[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("layers", [1, 2])
async def test_http_compressed_reply_is_an_error_and_is_not_expanded(layers, monkeypatch):
    """壓縮過的回應解開前看不出多大：直接算失敗，不解壓（上限算在收到的位元組上）。"""
    wire, size = _gzip_bomb(layers)
    encoding = ", ".join(["gzip"] * layers)
    fed: list[int] = []
    real_feed = runners._CappedReader.feed

    def spy(self, chunk: bytes) -> None:
        fed.append(len(chunk))
        real_feed(self, chunk)

    monkeypatch.setattr(runners._CappedReader, "feed", spy)
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, content=wire, headers={"Content-Encoding": encoding})
    )
    result = await run_http(_http(), b"{}", transport=transport)
    assert len(wire) < MAX_HOOK_OUTPUT_BYTES < size
    assert (result.status, result.detail) == ("error", "compressed response")
    assert result.stdout == ""
    assert sum(fed) == 0


@pytest.mark.parametrize("encoding", ["identity", "Identity "])
async def test_http_identity_encoding_is_read_as_is(encoding):
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, content=b'{"ok": 1}', headers={"Content-Encoding": encoding})
    )
    result = await run_http(_http(), b"{}", transport=transport)
    assert (result.status, result.stdout) == ("ok", '{"ok": 1}')


async def test_http_client_ignores_environment_credentials(monkeypatch):
    """不跟重定向、``trust_env=False``：不讀 .netrc 與代理設定，hook 網址拿不到其他地方的帳密。"""
    seen: dict[str, object] = {}
    real_client = httpx.AsyncClient

    def spy(**kwargs):
        seen.update(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(runners.httpx, "AsyncClient", spy)
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    await run_http(_http(), b"{}", transport=transport)
    assert seen["follow_redirects"] is False
    assert seen["trust_env"] is False
