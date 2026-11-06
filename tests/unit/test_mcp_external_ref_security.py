"""GHSA-rwrf-2pqf-9j8j 回歸測試：MCP client 驗證工具結果時，不得抓伺服器指定的外部 ``$ref``。

公告（2026-09-28 發布，無 CVE）：mcp 2.0.0–2.1.x 的 client 依伺服器的 ``outputSchema``
驗證 ``call_tool`` 結果時，jsonschema 預設 resolver 會同步、無逾時地抓 http／https／file
的 ``$ref``；2.2.0 改用空 registry 修正。這筆公告不在 OSV／PyPI，Guard #22（pip-audit）
看不到，本檔是它唯一的守門員（ADR-0203 dep1 補記、SECURITY.md）。

全離線：in-process 假伺服器；攔截 urllib 的取用路徑，只記錄、不連線，也不會卡住。
"""

from __future__ import annotations

import asyncio
import urllib.request
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from jsonschema.validators import validator_for
from mcp import Client, types
from mcp.server import Server
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation.evolution_command import EvolutionLaunch
from src.services.isolation.profiles import SandboxProfile
from src.services.isolation.runners import DirectRunner
from src.tools.builtin import evolution

if TYPE_CHECKING:
    from collections.abc import Awaitable

_CALL_TIMEOUT_SECONDS = 10.0
_EXTERNAL_REFS = (
    "http://ghsa-rwrf-2pqf-9j8j.invalid/schema.json",
    "https://ghsa-rwrf-2pqf-9j8j.invalid/schema.json",
    "file:///ghsa-rwrf-2pqf-9j8j/schema.json",
)


@pytest.mark.parametrize("ref", _EXTERNAL_REFS)
@pytest.mark.parametrize("windows", [True, False])
async def test_evolution_verification_never_fetches_external_ref(
    fetched, ref, windows, monkeypatch, zone_data_root,
):
    monkeypatch.setattr(EvolutionPathGuard, "is_windows", lambda: windows)
    root = Path(EvolutionPathGuard.get_tool_root("sample"))
    root.mkdir(parents=True, exist_ok=True)
    launch = EvolutionLaunch(DirectRunner(), SandboxProfile(
        name="evolution", cwd=root, rules=(), block_ports=(),
    ))
    monkeypatch.setattr(evolution, "acquire_evolution_launch", AsyncMock(return_value=launch))

    async def connect(*args, **kwargs):
        stack = AsyncExitStack()
        client = await stack.enter_async_context(Client(_server(ref), cache=None))
        return client, stack

    @asynccontextmanager
    async def local_client(*args, **kwargs):
        client, stack = await connect()
        async with stack:
            yield client

    monkeypatch.setattr(evolution, "connect_mcp_server", connect)
    monkeypatch.setattr(evolution, "_windows_client", local_client)
    result = await evolution._verify_mcp("sample", "probe", {})
    assert not result.passed and "RuntimeError" in result.text
    assert fetched == []


def _server(ref: str) -> Server:
    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="probe",
                    input_schema={"type": "object"},
                    output_schema={"$ref": ref},
                )
            ]
        )

    async def call_tool(_ctx: Any, _params: Any) -> types.CallToolResult:
        return types.CallToolResult(content=[], structured_content={"value": 1})

    return Server(
        "ghsa-rwrf-2pqf-9j8j",
        version="1.0.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """攔截 urllib 的取用路徑：只記下網址並立即失敗，不連線、不讀檔。"""
    seen: list[str] = []

    def _record(target: Any, *_args: Any, **_kwargs: Any) -> Any:
        seen.append(str(getattr(target, "full_url", target)))
        msg = "GHSA-rwrf-2pqf-9j8j regression test blocked an external $ref fetch"
        raise OSError(msg)

    monkeypatch.setattr(urllib.request, "urlopen", _record)
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda _self, target, *args, **kwargs: _record(target),
    )
    return seen


async def _outcome(call: Awaitable[Any]) -> BaseException | None:
    """執行一次呼叫並回傳例外（斷言在呼叫端）；卡住超過上限直接判失敗。"""
    try:
        (result,) = await asyncio.wait_for(
            asyncio.gather(call, return_exceptions=True), timeout=_CALL_TIMEOUT_SECONDS
        )
    except TimeoutError:
        pytest.fail("GHSA-rwrf-2pqf-9j8j: call_tool hung while validating the output schema")
    return result if isinstance(result, BaseException) else None


@pytest.mark.parametrize("ref", _EXTERNAL_REFS)
def test_ghsa_rwrf_2pqf_9j8j_interception_covers_jsonschema_default_retrieval(fetched, ref):
    """金絲雀：jsonschema 預設 resolver 確實走被攔截的路徑，否則下面的測試會空轉變綠。"""
    schema = {"$ref": ref}
    validator = validator_for(schema)(schema)

    with pytest.raises(Exception):  # noqa: B017, PT011 - 型別隨 referencing 版本
        validator.is_valid({"value": 1})

    assert fetched == [ref]


@pytest.mark.parametrize("ref", _EXTERNAL_REFS)
@pytest.mark.parametrize("listed_first", [False, True], ids=["sdk-self-lists", "listed-first"])
async def test_ghsa_rwrf_2pqf_9j8j_client_call_tool_never_fetches_external_ref(
    fetched,
    ref,
    listed_first,
):
    async with Client(_server(ref), cache=None) as client:
        if listed_first:
            await client.list_tools()
        outcome = await _outcome(client.call_tool("probe", {}))

    assert fetched == []
    assert isinstance(outcome, RuntimeError)
    assert "probe" in str(outcome)


@pytest.mark.parametrize("ref", _EXTERNAL_REFS)
async def test_ghsa_rwrf_2pqf_9j8j_low_level_session_never_fetches_external_ref(fetched, ref):
    """Touka 的 modern 路徑直接用 ``client.session.call_tool``（MRTR driver）。"""
    async with Client(_server(ref), cache=None) as client:
        outcome = await _outcome(
            client.session.call_tool(
                "probe",
                {},
                allow_input_required=True,
                allow_claimed=True,
            )
        )

    assert fetched == []
    assert isinstance(outcome, RuntimeError)
    assert "probe" in str(outcome)
