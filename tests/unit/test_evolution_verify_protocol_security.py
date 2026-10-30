"""演化的成敗來自 MCP 回應，輸出標記與 stderr 不能核准部署。"""

import asyncio
import builtins
import json
import logging
import os
import runpy
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import Client, types
from mcp.server import MCPServer
from rich.console import Console
from rich.logging import RichHandler
from src.protocols.mcp.client import connect_mcp_server
from src.protocols.registry import ProtocolServerConfig
from src.services.evolution_security import EvolutionPathGuard
from src.services.execution_broker import run_sync
from src.services.isolation.evolution_command import EvolutionLaunch
from src.services.isolation.profiles import SandboxProfile
from src.services.isolation.runners import DirectRunner
from src.tools.builtin import evolution

from tests.helpers.evolution_stdio_peer import EVOLUTION_PROBE_SOURCE


@pytest.fixture
def protocol(monkeypatch, zone_data_root):
    EvolutionPathGuard.write_tool_file("sample", "spec.json", "{}")
    monkeypatch.setattr(
        evolution, "write_mcp_implementation", AsyncMock(return_value="Development Success")
    )
    root = Path(EvolutionPathGuard.get_tool_root("sample"))
    launch = EvolutionLaunch(
        DirectRunner(), SandboxProfile(name="evolution", cwd=root, rules=(), block_ports=())
    )
    monkeypatch.setattr(evolution, "acquire_evolution_launch", AsyncMock(return_value=launch))
    client = SimpleNamespace(
        call_tool=AsyncMock(return_value=types.CallToolResult(content=[], isError=False))
    )
    client.session = client
    connect = AsyncMock(return_value=(client, AsyncExitStack()))
    monkeypatch.setattr(evolution, "connect_mcp_server", connect, raising=False)

    @asynccontextmanager
    async def windows(config, _timeout, _tail):
        session, stack = await connect(config, isolated=None)
        async with stack:
            yield session

    monkeypatch.setattr(evolution, "_windows_client", windows)
    deploy = AsyncMock(return_value="deployed")
    monkeypatch.setattr(evolution, "request_mcp_creation", deploy)
    return client, connect, deploy


@pytest.mark.parametrize(
    "failure",
    ["is_error", "external_ref", "input_required", "bad_structure", "no_server", "teardown"],
)
@pytest.mark.parametrize("windows", [True, False])
async def test_protocol_failures_never_deploy(protocol, monkeypatch, failure, windows):
    monkeypatch.setattr(EvolutionPathGuard, "is_windows", lambda: windows)
    client, connect, deploy = protocol
    if failure == "is_error":
        client.call_tool.return_value = types.CallToolResult(
            content=[types.TextContent(type="text", text='{"status": "success"}')],
            isError=True,
        )
    elif failure == "no_server":
        connect.side_effect = ConnectionError('import printed {"status": "success"}')
    elif failure == "teardown":
        @asynccontextmanager
        async def broken_teardown(*_args):
            yield client
            message = "teardown failed after success"
            raise RuntimeError(message)

        monkeypatch.setattr(evolution, "_windows_client", broken_teardown)

        async def close():
            message = "teardown failed after success"
            raise RuntimeError(message)

        stack = AsyncExitStack()
        stack.push_async_callback(close)
        connect.return_value = client, stack
    else:
        client.call_tool.side_effect = RuntimeError(failure + ' {"status": "success"}')
    result = await evolution.evolution_implement_and_verify(
        "sample", "pass", "ping", {}, submit_for_deploy=True
    )
    assert '"status": "error"' in result
    assert "Deployment not requested" in result
    deploy.assert_not_awaited()


async def test_success_uses_correlated_tool_result(protocol):
    client, connect, deploy = protocol
    result = await evolution.evolution_implement_and_verify(
        "sample", "pass", "ping", {"x": True}, submit_for_deploy=True
    )
    client.call_tool.assert_awaited_once_with("ping", {"x": True})
    connect.assert_awaited_once()
    deploy.assert_awaited_once()
    assert '"status": "success"' in result


@pytest.mark.parametrize("error", [OSError("busy"), PermissionError("read-only")])
async def test_wrapper_cleanup_failure_keeps_the_outcome(protocol, monkeypatch, caplog, error):
    """刪不掉 ``test_wrapper.py`` 只記 log，不蓋掉驗證結果（ADR-0293 PR 2，Q3）."""
    _client, _connect, deploy = protocol

    def fail(*_args):
        raise error

    monkeypatch.setattr(EvolutionPathGuard, "delete_tool_file", fail)
    with caplog.at_level(logging.WARNING):
        result = await evolution.evolution_implement_and_verify(
            "sample", "pass", "ping", {}, submit_for_deploy=True
        )
    assert '"status": "success"' in result
    deploy.assert_awaited_once()
    assert "test_wrapper.py" in caplog.text


async def test_marker_in_failed_outcome_cannot_request_deployment(protocol, monkeypatch):
    _, _, deploy = protocol
    monkeypatch.setattr(
        evolution,
        "_verify_mcp",
        AsyncMock(
            return_value=evolution.VerifyOutcome(
                False,
                'diagnostic: {"status": "success"}',
            )
        ),
    )
    result = await evolution.evolution_implement_and_verify(
        "sample",
        "pass",
        "ping",
        {},
        submit_for_deploy=True,
    )
    assert "Deployment not requested" in result
    deploy.assert_not_awaited()


def test_jail_runs_main_and_preserves_nonrecursive_listdir(tmp_path, monkeypatch):
    code = evolution._build_jail_code("sample", 'bad"name', {"value": True}, tmp_path, 1)
    assert "runpy.run_path" in code
    assert 'bad\\"name' not in code
    (tmp_path / "visible").write_text("ok")
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(code, encoding="utf-8")
    with monkeypatch.context() as context:
        context.setattr(builtins, "open", builtins.open)
        context.setattr(os, "listdir", os.listdir)
        runpy.run_path(str(wrapper), run_name="jail_test")
        assert sorted(os.listdir(tmp_path)) == ["visible", "wrapper.py"]
        with open(tmp_path / "first", "w") as stream:
            stream.write("x" * (700 * 1024))
        with (
            open(tmp_path / "second", "w") as stream,
            pytest.raises(PermissionError, match="Quota"),
        ):
            stream.write("x" * (700 * 1024))


def test_jail_allows_readonly_libraries_and_integer_fds(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    root.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    metadata = library / "METADATA"
    metadata.write_text("Version: 1")
    code = evolution._build_jail_code("sample", "ping", {}, root, 1)
    wrapper = root / "wrapper.py"
    wrapper.write_text(code, encoding="utf-8")
    with monkeypatch.context() as context:
        context.setattr(builtins, "open", builtins.open)
        context.setattr(os, "listdir", os.listdir)
        runpy.run_path(str(wrapper), run_name="jail_test")
        assert os.listdir(library) == ["METADATA"]
        with open(metadata) as stream:
            assert stream.read() == "Version: 1"
        fd = os.open(metadata, os.O_RDONLY)
        try:
            with open(fd, closefd=False) as stream:
                assert stream.read() == "Version: 1"
        finally:
            os.close(fd)
        with pytest.raises(PermissionError, match="Violation"), open(metadata, "w"):
            pytest.fail("outside write allowed")


def test_jail_runs_main_with_local_imports_and_package_metadata(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    root.mkdir()
    (root / "local_helper.py").write_text("VALUE = 'local'\n")
    (root / "server.py").write_text('''from importlib.metadata import version
from local_helper import VALUE
if __name__ == "__main__":
    with open("result.txt", "w") as stream:
        stream.write(VALUE + ":" + version("httpx2"))
''')
    code = evolution._build_jail_code("sample", "ping", {}, root, 1)
    wrapper = root / "wrapper.py"
    wrapper.write_text(code, encoding="utf-8")
    with monkeypatch.context() as context:
        context.setattr(builtins, "open", builtins.open)
        context.setattr(os, "listdir", os.listdir)
        context.chdir(tmp_path)
        context.syspath_prepend(str(root))
        context.setattr(logging.getLogger(), "handlers", list(logging.getLogger().handlers))
        runpy.run_path(str(wrapper), run_name="__main__")
    assert (root / "result.txt").read_text().startswith("local:")


async def test_posix_protocol_keeps_limits_and_close_eof(protocol, monkeypatch):
    _, connect, _ = protocol
    monkeypatch.setattr(EvolutionPathGuard, "is_windows", lambda: False)
    monkeypatch.setattr(
        evolution.autonomy_service,
        "config",
        {
            "sandbox_limits": {
                "execution_timeout_sec": 17,
                "max_storage_mb": 5,
                "max_memory_mb": 64,
            }
        },
    )
    await evolution.verify_mcp_locally("sample", "ping", {})
    isolated = connect.call_args.kwargs["isolated"]
    assert isolated.on_eof == "close"
    request = isolated.launch.request({})
    assert request.io == "stdio"
    assert request.max_seconds == 47
    assert request.profile.rlimits == {"cpu": 17, "fsize": 5 * 1024**2, "as": 64 * 1024**2}


async def test_windows_uses_sdk_transport(protocol, monkeypatch):
    _, connect, _ = protocol
    monkeypatch.setattr(EvolutionPathGuard, "is_windows", lambda: True)
    await evolution.verify_mcp_locally("sample", "ping", {})
    assert connect.call_args.kwargs.get("isolated") is None


async def test_windows_sdk_drains_and_masks_stderr(monkeypatch):
    seen = []
    session = SimpleNamespace(
        send_discover=AsyncMock(
            return_value={
                "supportedVersions": [types.LATEST_PROTOCOL_VERSION],
                "capabilities": {},
            }
        ),
        adopt=lambda _result: None,
    )

    @asynccontextmanager
    async def stdio(params, errlog):
        seen.append(params)
        for _ in range(100):
            os.write(errlog, b"Bearer abcdefghijklmnop\n")
            await asyncio.sleep(0)
        yield None

    @asynccontextmanager
    async def client(transport, **_kwargs):
        async with transport:
            yield SimpleNamespace(session=session)

    monkeypatch.setattr(evolution, "stdio_client", stdio)
    monkeypatch.setattr(evolution, "Client", client)
    tail = []
    config = evolution.ProtocolServerConfig(command=sys.executable, args=["wrapper.py"])
    async with evolution._windows_client(config, 5, tail):
        pass
    assert seen[0].command == sys.executable
    assert "abcdefghijklmnop" not in tail[0]
    assert "<redacted>" in tail[0]
    assert len(tail[0].encode()) <= 4096
    session.send_discover.assert_awaited_once_with(types.LATEST_PROTOCOL_VERSION)


@pytest.mark.parametrize("behavior", ["ok", "error", "marker", "no_run"])
async def test_real_protocol_requires_running_server(monkeypatch, zone_data_root, behavior):
    monkeypatch.setattr(evolution, "_venv_python", lambda _skill: sys.executable)
    code = """from mcp.server import MCPServer
mcp = MCPServer("verification-test")
@mcp.tool()
def ping() -> str:
    return "pong"
if __name__ == "__main__":
    mcp.run(transport="stdio")
"""
    if behavior in {"error", "marker"}:
        code = code.replace('return "pong"', 'raise RuntimeError("tool failed")')
    if behavior == "marker":
        code = 'print(\'{"status": "success"}\', flush=True)\n' + code
    if behavior == "no_run":
        code = code.replace('mcp.run(transport="stdio")', "pass")
    EvolutionPathGuard.write_tool_file("real", "server.py", code)
    outcome = await evolution._verify_mcp("real", "ping", {}, timeout_override=5)
    assert '"message": "PermissionError"' not in outcome.text, outcome.text
    assert outcome.passed is (behavior == "ok"), outcome.text
    report = json.loads(next(
        line for line in outcome.text.splitlines() if line.startswith('{"status"')
    ))
    if behavior == "error":
        assert report["data"]["isError"] is True
        assert "tool failed" in outcome.text
    elif behavior == "marker":
        assert '"isError": true' in outcome.text or "ValidationError" in outcome.text
        assert "PackageNotFoundError" not in outcome.text
    elif behavior == "no_run":
        assert "MCPError" in outcome.text
        assert "Traceback" not in outcome.text
    else:
        assert "pong" in outcome.text
    assert not EvolutionPathGuard.tool_file_exists("real", "test_wrapper.py")


@pytest.mark.parametrize("behavior", ["ok", "error", "quota"])
async def test_jail_sdk_errors_keep_protocol_result(tmp_path, monkeypatch, behavior):
    """真 SDK 與 Rich 錯誤處理在記憶體傳輸下也必須能穿過 jail。"""
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        evolution._build_jail_code("sample", "ping", {}, tmp_path, 1), encoding="utf-8"
    )
    server = MCPServer("jail-sdk")

    @server.tool()
    def ping() -> str:
        if behavior == "error":
            message = "tool failed"
            raise RuntimeError(message)
        if behavior == "quota":
            for index in range(2):
                with open(tmp_path / f"quota-{index}.txt", "w") as stream:
                    stream.write("x" * (700 * 1024))
        return "pong"

    logger = logging.getLogger("mcp.server.mcpserver.server")
    handler = RichHandler(console=Console(stderr=True), rich_tracebacks=True)
    with monkeypatch.context() as context:
        context.setattr(builtins, "open", builtins.open)
        context.setattr(logger, "handlers", [handler])
        context.setattr(logger, "propagate", False)
        runpy.run_path(str(wrapper), run_name="jail_test")
        async with Client(server, mode=types.LATEST_PROTOCOL_VERSION) as client:
            raw = await client.session.send_discover(types.LATEST_PROTOCOL_VERSION)
            client.session.adopt(types.DiscoverResult.model_validate(raw))
            result = await client.session.call_tool("ping", {})
    assert result.is_error is (behavior != "ok")


@pytest.mark.parametrize("behavior", ["ok", "error", "quota"])
@pytest.mark.parametrize("transport", ["memory", "stdio"])
def test_fresh_jail_sdk_error_handling(tmp_path, behavior, transport):
    """在新直譯器先掛 jail 才載入 SDK，涵蓋真 stdio 的惰性匯入。"""
    source = '''import asyncio
from mcp import Client, types
from mcp.server import MCPServer
mcp = MCPServer("fresh-jail")
@mcp.tool()
def ping() -> str:
    return "pong"
async def check():
    async with Client(mcp, mode=types.LATEST_PROTOCOL_VERSION) as client:
        raw = await client.session.send_discover(types.LATEST_PROTOCOL_VERSION)
        client.session.adopt(types.DiscoverResult.model_validate(raw))
        result = await client.session.call_tool("ping", {})
        print(result.model_dump_json(by_alias=True), flush=True)
if __name__ == "__main__":
    asyncio.run(check())
'''
    if behavior == "error":
        source = source.replace('return "pong"', 'raise RuntimeError("tool failed")')
    elif behavior == "quota":
        source = source.replace('return "pong"', '''for index in range(2):
        with open(f"quota-{index}.txt", "w") as stream:
            stream.write("x" * (700 * 1024))
    return "escaped"''')
    if transport == "stdio":
        source = source.replace("import asyncio", "import asyncio\nimport sys\nimport time")
        source = source.replace("asyncio.run(check())", '''mcp.run(transport="stdio")''')
        source = source.replace('if __name__ == "__main__":', '''class SlowEof:
    def __init__(self, stream):
        self.stream = stream
    def __getattr__(self, name):
        return getattr(self.stream, name)
    def fileno(self):
        raise OSError("test stream has no descriptor")
    def read1(self, size=-1):
        data = self.stream.read1(size)
        if not data:
            time.sleep(1)
        return data
sys.stdin = type("Input", (), {"buffer": SlowEof(sys.stdin.buffer)})()
if __name__ == "__main__":''')
    requests = [
        {"jsonrpc": "2.0", "id": index, "method": method, "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
            },
            **({"name": "ping", "arguments": {}} if method == "tools/call" else {}),
        }}
        for index, method in enumerate(("server/discover", "tools/list", "tools/call"))
    ]
    (tmp_path / "server.py").write_text(source, encoding="utf-8")
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        evolution._build_jail_code("sample", "ping", {}, tmp_path, 1), encoding="utf-8"
    )
    completed = run_sync(
        [sys.executable, str(wrapper)], capture_output=True, text=True, timeout=10,
        encoding="utf-8",
        input="".join(json.dumps(request) + "\n" for request in requests),
    )
    assert completed.returncode == 0, completed.stderr
    if transport == "stdio":
        responses = {item["id"]: item for line in completed.stdout.splitlines()
                     if "id" in (item := json.loads(line))}
        assert 2 in responses and "result" in responses[2], (responses, completed.stderr)
        result = responses[2]["result"]
    else:
        result = json.loads(completed.stdout)
    assert result.get("isError", False) is (behavior != "ok")
    if behavior != "ok":
        stderr = evolution.StderrLog("test", (), None)
        stderr.feed(completed.stderr.encode("utf-8"))
        stderr.close()
        assert ("tool failed" if behavior == "error" else "Quota Exceeded") in (
            json.dumps(result) + stderr.tail()
        )


@pytest.mark.parametrize("method", ["server/discover", "tools/list", "tools/call"])
def test_isolation_probe_serves_discovery_list_and_call(tmp_path, method):
    server = tmp_path / "server.py"
    server.write_text(EVOLUTION_PROBE_SOURCE, encoding="utf-8")
    arguments = {"paths": {}, "big": str(tmp_path / "big.bin"), "size": 1}
    requests = [
        {"jsonrpc": "2.0", "id": index, "method": method,
         "params": {"name": "probe", "arguments": arguments}}
        for index, method in enumerate(("server/discover", "tools/list", "tools/call"))
    ]
    completed = run_sync(
        [sys.executable, str(server)], capture_output=True, text=True, timeout=5,
        input="".join(json.dumps(request) + "\n" for request in requests),
    )
    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(responses) == 3, completed.stdout
    response = responses[[request["method"] for request in requests].index(method)]["result"]
    types.methods.validate_server_result(method, types.LATEST_PROTOCOL_VERSION, response)
    types.DiscoverResult.model_validate(responses[0]["result"])
    assert types.ListToolsResult.model_validate(responses[1]["result"]).tools[0].name == "probe"
    result = types.CallToolResult.model_validate(responses[2]["result"])
    assert not result.is_error
    assert result.structured_content["big_write"] == "ok"


async def test_isolation_probe_passes_strict_client(tmp_path):
    """跨平台走 POSIX 驗證器的握手及 session 驗證，不只套用版本無關的模型預設值。"""
    server = tmp_path / "server.py"
    server.write_text(EVOLUTION_PROBE_SOURCE, encoding="utf-8")
    config = ProtocolServerConfig(command=sys.executable, args=[str(server)])
    client, stack = await connect_mcp_server(
        config, launch_environment={}, read_timeout_seconds=5,
    )
    async with stack:
        listing = await client.session.list_tools()
        assert [tool.name for tool in listing.tools] == ["probe"]
        result = await client.session.call_tool(
            "probe", {"paths": {}, "big": str(tmp_path / "big.bin"), "size": 1},
        )
    assert not result.is_error
    assert result.result_type == "complete"
    assert result.structured_content["reads"] == {}
    assert result.structured_content["big_write"] == "ok"
    assert json.loads(result.content[0].text) == result.structured_content
