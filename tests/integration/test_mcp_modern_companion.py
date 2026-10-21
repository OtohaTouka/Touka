"""Real Proxy -> Node IDE modern companion; no VS Code or live workspace.

This exercises the modern-only source candidate, not a live cutover or an
IDE-host qualification. The executor operates only on synthetic memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import shutil
import subprocess
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mcp import types
from mcp.shared.exceptions import MCPError
from src.config import get_full_env
from src.protocols.ide.handler import call_ide_tool
from src.protocols.mcp.client import connect_mcp_server
from src.protocols.registry import ProtocolServerConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

logger = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[2]
IDE = REPO / "ToukaIDE"
ENTRY = IDE / "src" / "__tests__" / "fixtures" / "modernCompanion.ts"
MODERN_VERSION = "2026-07-28"
COMPILE_SECONDS = 30
READY_SECONDS = 10
CASE_SECONDS = 30
CLEANUP_SECONDS = 5
PIPE_LIMIT = 64 * 1024


def _identity(path: Path) -> dict[str, Any]:
    before = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    after = path.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ), "Companion input changed while reading"
    return {"path": str(path), "sha256": digest, "size": after.st_size}


def _record(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


@dataclass
class _Bundle:
    node: Path
    script: Path
    environment: dict[str, str]
    inputs: list[dict[str, Any]]


@pytest.fixture(scope="module")
def companion_bundle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Bundle]:
    """Use installed tools directly; never install or invoke package scripts."""
    node_name = shutil.which("node")
    assert node_name is not None, "Modern companion requires the existing Node runtime"
    node = Path(node_name).resolve(strict=True)
    root = tmp_path_factory.mktemp("modern_companion_bundle")
    parent_env = {key.casefold(): value for key, value in get_full_env().items()}
    environment = {
        key: parent_env[key.casefold()]
        for key in ("SystemRoot", "WINDIR", "COMSPEC")
        if key.casefold() in parent_env
    }
    environment.update(PATH=str(node.parent), TMP=str(root), TEMP=str(root), HOME=str(root))
    package = json.loads((IDE / "package.json").read_text(encoding="utf-8"))
    # Fixed installed Node, no shell, clean environment, 10-second timeout.
    version = subprocess.run(  # noqa: S603
        [str(node), "--version"],
        check=True,
        capture_output=True,
        env=environment,
        cwd=root,
        timeout=READY_SECONDS,
        shell=False,
    )
    assert version.stdout.decode().strip() == "v" + package["devEngines"]["runtime"]["version"]
    assert ENTRY.is_file(), "Modern companion fixture is not implemented"
    script = root / "modernCompanion.cjs"
    metafile = root / "bundle-metafile.json"
    esbuild = IDE / "node_modules" / "esbuild" / "bin" / "esbuild"
    command = [
        str(node),
        str(esbuild),
        str(ENTRY),
        "--bundle",
        "--platform=node",
        "--format=cjs",
        f"--outfile={script}",
        f"--metafile={metafile}",
    ]
    # Fixed installed esbuild/fixture, no shell, clean environment, 30-second timeout.
    compiled = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        env=environment,
        cwd=IDE,
        timeout=COMPILE_SECONDS,
        shell=False,
    )
    (root / "compile.stdout.bin").write_bytes(compiled.stdout)
    (root / "compile.stderr.bin").write_bytes(compiled.stderr)
    _record(root / "compile.json", {"argv": command, "returncode": compiled.returncode})
    assert compiled.returncode == 0, "Modern companion bundle failed; see compile evidence"
    metadata = json.loads(metafile.read_text(encoding="utf-8"))
    inputs = []
    for relative in sorted(metadata["inputs"]):
        path = (IDE / relative).resolve(strict=True)
        assert path.is_relative_to(IDE) and "data" not in path.relative_to(IDE).parts
        assert path.name != ".gitignore"
        inputs.append(_identity(path))
    assert not any("vscode" in row.get("path", "").lower() for row in inputs)
    inputs.extend(
        _identity(path)
        for path in (
            node,
            esbuild,
            IDE / "package.json",
            IDE / "package-lock.json",
            REPO / "uv.lock",
        )
    )
    _record(root / "inputs-before-cases.json", inputs)
    _record(root / "bundle.json", _identity(script))
    logger.info("Modern companion bundle evidence: %s", root)
    try:
        yield _Bundle(node, script, environment, inputs)
    finally:
        after = [_identity(Path(row["path"])) for row in inputs]
        _record(root / "inputs-after-cases.json", after)
        assert inputs == after, "Modern companion compiled input drift"


@dataclass
class _Peer:
    process: asyncio.subprocess.Process
    token: str = field(repr=False)
    events: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""
    pipe_error: bool = False
    stderr_size: int = 0

    async def write(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()

    async def event(self, expected: str, timeout: float = READY_SECONDS) -> dict[str, Any]:
        event = await asyncio.wait_for(self.events.get(), timeout)
        assert event.get("event") == expected, "Unexpected companion control event"
        return event

    async def snapshot(self) -> dict[str, Any]:
        await self.write({"command": "snapshot"})
        return await self.event("snapshot")

    async def collect_stdout(self) -> None:
        assert self.process.stdout is not None
        total = 0
        try:
            while line := await self.process.stdout.readline():
                total += len(line)
                if total > PIPE_LIMIT or self.token.encode() in line:
                    self.pipe_error = True
                    break
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("event") not in {
                    "ready",
                    "snapshot",
                    "started",
                    "cancelled",
                    "closed",
                    "failed",
                }:
                    self.pipe_error = True
                    break
                self.transcript.append(event)
                await self.events.put(event)
        except (ValueError, UnicodeError):
            self.pipe_error = True
        if self.pipe_error:
            await self.events.put({"event": "failed"})

    async def collect_stderr(self) -> None:
        assert self.process.stderr is not None
        while chunk := await self.process.stderr.read(4096):
            self.stderr_size += len(chunk)
            if self.stderr_size > PIPE_LIMIT:
                self.pipe_error = True
                return


@asynccontextmanager
async def _running_peer(bundle: _Bundle, output: Path) -> AsyncIterator[_Peer]:
    """Own one child; no SDK context crosses fixture task boundaries."""
    process = await asyncio.create_subprocess_exec(
        str(bundle.node),
        "--no-warnings",
        str(bundle.script),
        cwd=output,
        env=bundle.environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=PIPE_LIMIT,
    )
    peer = _Peer(process, secrets.token_urlsafe(32))
    readers = [
        asyncio.create_task(peer.collect_stdout()),
        asyncio.create_task(peer.collect_stderr()),
    ]
    clean = False
    forced = False
    try:
        await peer.write({"command": "start", "token": peer.token})
        ready = await peer.event("ready")
        assert isinstance(ready.get("port"), int) and 0 < ready["port"] < 65536
        peer.url = f"http://127.0.0.1:{ready['port']}/mcp"
        # asyncio.timeout preserves the task owning all SDK cancel scopes.
        async with asyncio.timeout(CASE_SECONDS):
            yield peer
    finally:
        try:
            async with asyncio.timeout(CLEANUP_SECONDS):
                if process.returncode is None:
                    await peer.write({"command": "stop"})
                    # A failed body may leave already-recorded terminal events queued.
                    # Still read the real cleanup result instead of masking that failure.
                    while (closed := await peer.events.get()).get("event") != "closed":
                        if closed.get("event") == "failed":
                            peer.pipe_error = True
                    clean = closed == {"event": "closed", "warnings": 0, "sockets": 0, "active": 0}
                    assert process.stdin is not None
                    process.stdin.close()
                await process.wait()
                await asyncio.gather(*readers)
        except (TimeoutError, ConnectionError, AssertionError):
            clean = False
        finally:
            if process.returncode is None:
                forced = True
                process.kill()
                await asyncio.wait_for(process.wait(), CLEANUP_SECONDS)
            for task in readers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            _record(
                output / "companion-receipt.json",
                {
                    "events": peer.transcript,
                    "returncode": process.returncode,
                    "stderr_bytes": peer.stderr_size,
                    "pipe_error": peer.pipe_error,
                    "forced_termination": forced,
                    "clean": clean,
                },
            )
            assert clean and not forced and not peer.pipe_error and peer.stderr_size == 0, (
                "Modern companion cleanup or bounded output failed"
            )
            assert process.returncode == 0, "Modern companion child failed"


def _config(peer: _Peer, *, headers: dict[str, str] | None = None) -> ProtocolServerConfig:
    return ProtocolServerConfig(
        url=peer.url,
        protocol_mode=MODERN_VERSION,
        headers=headers if headers is not None else {"Authorization": "Bearer ${COMPANION_TOKEN}"},
        env={"COMPANION_TOKEN": peer.token},
    )


def _content(result: Any) -> list[dict[str, Any]]:
    return [item.model_dump(by_alias=True, exclude_none=True) for item in result.content]


@pytest.mark.asyncio
async def test_real_proxy_discovers_lists_calls_and_preserves_ide_result(
    companion_bundle: _Bundle,
    tmp_path: Path,
) -> None:
    async with _running_peer(companion_bundle, tmp_path) as peer:
        client, stack = await connect_mcp_server(_config(peer), read_timeout_seconds=5)
        try:
            assert client.protocol_version == MODERN_VERSION
            assert client.session.discover_result is not None
            assert client.session.initialize_result is None
            assert client.session.discover_result.supported_versions == [MODERN_VERSION]
            assert client.session.discover_result.capabilities.tools is not None
            assert client.session.server_info is not None
            assert client.session.server_info.name == "touka-vscode-mcp"
            assert client.session.server_info.version == "2.0.0"
            tools = await client.list_tools()
            by_name = {tool.name: tool for tool in tools.tools}
            assert {"get_open_file", "get_diagnostics", "execute_terminal"} <= by_name.keys()
            assert by_name["get_diagnostics"].input_schema["properties"]["uri"]["type"] == "string"
            assert by_name["execute_terminal"].input_schema["required"] == ["command"]
            result = await client.call_tool("get_open_file", {})
            expected = [{"type": "text", "text": '桃花\n{"value":null,"flags":[false,0]}'}]
            assert _content(result) == expected and result.is_error is False
            before = await peer.snapshot()
            rejected = await client.call_tool("execute_terminal", {})
            assert rejected.is_error is True
            after = await peer.snapshot()
            assert after["executions"] == before["executions"]
        finally:
            await stack.aclose()
        adapter_result = await call_ide_tool(
            {"ToukaUI": {"url": peer.url, "token": peer.token}},
            "get_open_file",
            {},
        )
        assert adapter_result == {"content": expected, "isError": False}
        observed = await peer.snapshot()
        assert observed["methods"]["server/discover"] == 2
        assert observed["methods"]["initialize"] == 0
        assert observed["legacy_requests"] == 0
        assert observed["executions"] == 2


@pytest.mark.asyncio
async def test_real_ide_accepts_touka_human_approval_marker_only_when_sent(
    companion_bundle: _Bundle,
    tmp_path: Path,
) -> None:
    """ADR-0246 U2c-7: Python mints, the real Node verifier accepts; no marker, no approval."""
    async with _running_peer(companion_bundle, tmp_path) as peer:
        clients = {"ToukaUI": {"url": peer.url, "token": peer.token}}
        probe = {"uri": "approval-probe", "agent_id": "Alice", "note": "桃花\n"}
        approved = await call_ide_tool(clients, "get_diagnostics", probe, human_approved=True)
        plain = await call_ide_tool(clients, "get_diagnostics", probe)
        assert approved == {
            "content": [{"type": "text", "text": "human-approved"}],
            "isError": False,
        }
        assert plain == {"content": [{"type": "text", "text": "not-approved"}], "isError": False}
        observed = await peer.snapshot()
        assert observed["executions"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["missing", "wrong"])
async def test_real_proxy_cannot_call_ide_without_valid_bearer(
    companion_bundle: _Bundle,
    tmp_path: Path,
    credential: str,
) -> None:
    async with _running_peer(companion_bundle, tmp_path) as peer:
        headers = {} if credential == "missing" else {"Authorization": "Bearer invalid-synthetic"}
        with pytest.raises(MCPError) as failure:
            client, stack = await connect_mcp_server(
                _config(peer, headers=headers), read_timeout_seconds=5
            )
            try:
                await client.call_tool("get_open_file", {})
            finally:
                await stack.aclose()
        assert failure.value.code == types.INTERNAL_ERROR
        observed = await peer.snapshot()
        assert observed["statuses"] == {"401": 1}
        assert observed["executions"] == 0
        assert observed["legacy_requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_header", ["Host", "Origin"])
async def test_real_proxy_valid_bearer_does_not_bypass_local_request_guard(
    companion_bundle: _Bundle,
    tmp_path: Path,
    bad_header: str,
) -> None:
    async with _running_peer(companion_bundle, tmp_path) as peer:
        headers = {
            "Authorization": "Bearer ${COMPANION_TOKEN}",
            bad_header: "attacker.example" if bad_header == "Host" else "https://attacker.example",
        }
        with pytest.raises(MCPError) as failure:
            client, stack = await connect_mcp_server(
                _config(peer, headers=headers), read_timeout_seconds=5
            )
            try:
                await client.call_tool("get_open_file", {})
            finally:
                await stack.aclose()
        assert failure.value.code == -32000
        observed = await peer.snapshot()
        assert observed["statuses"] == {"403": 1}
        assert observed["executions"] == 0
        assert observed["legacy_requests"] == 0


@pytest.mark.asyncio
async def test_two_real_proxy_clients_isolate_results_and_request_cancellation(
    companion_bundle: _Bundle,
    tmp_path: Path,
) -> None:
    async with _running_peer(companion_bundle, tmp_path) as peer:
        first_ready, second_ready = asyncio.Event(), asyncio.Event()

        async def first_client() -> None:
            client, stack = await connect_mcp_server(_config(peer), read_timeout_seconds=5)
            try:
                result = await client.call_tool("get_diagnostics", {"uri": "client-a"})
                assert _content(result) == [{"type": "text", "text": "client-a"}]
                first_ready.set()
                await second_ready.wait()
                await client.call_tool(
                    "get_diagnostics", {"uri": "hold-until-cancelled"}, read_timeout_seconds=20
                )
                pytest.fail("Cancelled companion call unexpectedly completed")
            finally:
                await stack.aclose()

        async def second_client() -> None:
            client, stack = await connect_mcp_server(_config(peer), read_timeout_seconds=5)
            try:
                result = await client.call_tool("get_diagnostics", {"uri": "client-b"})
                assert _content(result) == [{"type": "text", "text": "client-b"}]
                second_ready.set()
                result = await client.call_tool(
                    "get_diagnostics",
                    {"uri": "hold-until-released"},
                    read_timeout_seconds=20,
                )
                assert _content(result) == [{"type": "text", "text": "survivor-completed"}]
                result = await client.call_tool("get_diagnostics", {"uri": "client-b-still-active"})
                assert _content(result) == [{"type": "text", "text": "client-b-still-active"}]
            finally:
                await stack.aclose()

        first, second = asyncio.create_task(first_client()), asyncio.create_task(second_client())
        try:
            await asyncio.gather(first_ready.wait(), second_ready.wait())
            started = [await peer.event("started"), await peer.event("started")]
            assert {event["request"] for event in started} == {"cancelled-client", "survivor"}
            assert not first.done() and not second.done()
            assert (await peer.snapshot())["active"] == 2
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert await peer.event("cancelled") == {
                "event": "cancelled",
                "request": "cancelled-client",
            }
            assert not second.done()
            assert (await peer.snapshot())["active"] == 1
            await peer.write({"command": "release-survivor"})
            await second
            observed = await peer.snapshot()
            assert observed["cancelled"] == 1 and observed["active"] == 0
            assert observed["executions"] == 5 and observed["legacy_requests"] == 0
        finally:
            for task in (first, second):
                if not task.done():
                    task.cancel()
            for task in (first, second):
                with suppress(asyncio.CancelledError):
                    await task
