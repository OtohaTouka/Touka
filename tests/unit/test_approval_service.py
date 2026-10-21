"""H2 — 統一審批 broker 服務語意（hitl-unification）。

approval_service＝泛化 terminal 審批機殼（asyncio.Event＋單發解算＋逾時自動拒）：
register → wait（Gate-2 阻塞）→ resolve（人類端點）。本檔鎖服務本體的併發／單發／
逾時／容量語意；executor 接線見 test_hitl_broker_executor、端點見
test_tools_approval_endpoints。
"""

from __future__ import annotations

import asyncio

import pytest
from src.services import approval_service as approval_module
from src.services.approval_service import ApprovalService


@pytest.fixture
def service():
    svc = ApprovalService()
    yield svc
    svc.reset_for_test()


async def _register(svc: ApprovalService, exec_id: str = "e1") -> str:
    ok = await svc.register(
        exec_id,
        tool_name="write",
        agent_id="Sumi",
        session_id="sess-1",
        arguments_preview={"scope": "vault"},
    )
    assert ok
    return exec_id


@pytest.mark.asyncio
async def test_approve_wakes_waiter(service):
    exec_id = await _register(service)
    waiter = asyncio.create_task(service.wait(exec_id))
    await asyncio.sleep(0)
    assert await service.resolve(exec_id, approved=True, resolver="human-1")
    assert await waiter == "approved"
    assert service.get(exec_id) is None  # 解算後不可再查（單發）


@pytest.mark.asyncio
async def test_reject_wakes_waiter(service):
    exec_id = await _register(service)
    waiter = asyncio.create_task(service.wait(exec_id))
    await asyncio.sleep(0)
    assert await service.resolve(exec_id, approved=False, resolver="human-1")
    assert await waiter == "rejected"


@pytest.mark.asyncio
async def test_timeout_auto_rejects_fail_closed(service):
    exec_id = await _register(service)
    verdict = await service.wait(exec_id, timeout_s=0.05)
    assert verdict == "timeout"
    assert service.get(exec_id) is None  # 逾時即清除，之後 resolve 必 False
    assert not await service.resolve(exec_id, approved=True, resolver="late")


@pytest.mark.asyncio
async def test_resolve_is_single_shot(service):
    exec_id = await _register(service)
    waiter = asyncio.create_task(service.wait(exec_id))
    await asyncio.sleep(0)
    assert await service.resolve(exec_id, approved=True, resolver="first")
    assert not await service.resolve(exec_id, approved=False, resolver="second")
    assert await waiter == "approved"  # 第二次解算不得翻盤


@pytest.mark.asyncio
async def test_unknown_exec_id(service):
    assert not await service.resolve("ghost", approved=True, resolver="x")
    assert service.get("ghost") is None
    assert await service.wait("ghost", timeout_s=0.05) == "rejected"


@pytest.mark.asyncio
async def test_duplicate_register_refused(service):
    await _register(service, "dup")
    ok = await service.register(
        "dup", tool_name="edit", agent_id="A", session_id=None, arguments_preview={}
    )
    assert not ok


@pytest.mark.asyncio
async def test_queue_cap_fail_closed(service, monkeypatch):
    monkeypatch.setattr(approval_module, "MAX_PENDING", 1)
    await _register(service, "one")
    ok = await service.register(
        "two", tool_name="edit", agent_id="A", session_id=None, arguments_preview={}
    )
    assert not ok  # 滿載＝拒收（呼叫端 fail-closed）


@pytest.mark.asyncio
async def test_concurrent_pendings_independent(service):
    a = await _register(service, "a")
    b = await _register(service, "b")
    wa = asyncio.create_task(service.wait(a))
    wb = asyncio.create_task(service.wait(b))
    await asyncio.sleep(0)
    await service.resolve(b, approved=False, resolver="h")
    await service.resolve(a, approved=True, resolver="h")
    assert await wa == "approved"
    assert await wb == "rejected"  # 並行多筆各自獨立（grok parallel calls 場景）


@pytest.mark.asyncio
async def test_structured_interaction_carries_schema_payload_and_resolution(service):
    ok = await service.register(
        "mcp-one",
        tool_name="server__demo",
        agent_id="Sumi",
        session_id="sess-1",
        arguments_preview={},
        interaction_kind="mcp_elicitation",
        interaction_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
        interaction_payload={"message": "Choose"},
    )
    assert ok
    pending = service.get("mcp-one")
    assert pending is not None
    assert pending.interaction_kind == "mcp_elicitation"
    assert pending.interaction_payload == {"message": "Choose"}

    waiter = asyncio.create_task(service.wait_for_resolution("mcp-one"))
    await asyncio.sleep(0)
    assert await service.resolve(
        "mcp-one",
        decision="accepted",
        resolver="human-1",
        response_payload={"answer": "yes"},
    )
    resolution = await waiter
    assert resolution.decision == "accepted"
    assert resolution.response_payload == {"answer": "yes"}


@pytest.mark.asyncio
async def test_structured_interaction_supports_explicit_cancel(service):
    await _register(service, "mcp-cancel")
    waiter = asyncio.create_task(service.wait_for_resolution("mcp-cancel"))
    await asyncio.sleep(0)
    assert await service.resolve(
        "mcp-cancel",
        decision="cancelled",
        resolver="human-1",
    )
    assert (await waiter).decision == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_waiter_cleans_ephemeral_interaction(service):
    await _register(service, "mcp-disconnect")
    waiter = asyncio.create_task(
        service.wait_for_resolution("mcp-disconnect", timeout_s=30)
    )
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert service.pending_count() == 0
