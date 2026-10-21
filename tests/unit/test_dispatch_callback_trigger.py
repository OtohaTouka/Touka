"""批⑤-E E29（ADR-0051）：ToukaDispatch 終態回呼 trigger——零 pytest 覆蓋補課。

server.py:399-418——`dispatch_update_task` 更新為終態（DONE/FAILED）且 mission
metadata 有對應 callback token 時，觸發 `_notify_touka_proxy`（解除 supervising
agent 的等待）。既有 DONE 路徑測試全繞過 ToukaDispatch＝終態分支從不進。

骨架前例：test_dispatch_identity_fixation（E04 的 dispatch_server fixture）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ToukaDispatch import server as ds

_NOTIFIED: list[tuple[str, str, str, str]] = []


class _SpyManager:
    """MissionManager 替身：回帶 callback 對照表的 mission、不碰檔案系統。"""

    async def get_current_mission(self, identity: Any = None, target_agent_id: Any = None) -> Any:
        return SimpleNamespace(
            mission_id="m-e29",
            metadata={"dispatch_callbacks": {"t1": "cb-tok-1"}},
        )

    async def update_task(self, identity: Any = None, **kw: Any) -> Any:
        return None


@pytest.fixture
def dispatch_server(monkeypatch):
    """spy MissionManager＋spy _notify_touka_proxy 的 dispatch server 模組。"""
    monkeypatch.setenv("DISPATCH_TOKEN", "dispatch-e29-token")
    monkeypatch.setattr(ds, "MissionManager", _SpyManager)

    async def _spy_notify(cb_token: str, task_id: str, status: str, summary: str) -> None:
        _NOTIFIED.append((cb_token, task_id, status, summary))

    monkeypatch.setattr(ds, "_notify_touka_proxy", _spy_notify)
    _NOTIFIED.clear()
    return ds


async def _handshake(server: Any) -> str:
    result = await server.dispatch_handshake(
        dispatch_token="dispatch-e29-token", caller_name="E29Tool"
    )
    assert "Session token:" in result, f"Handshake failed: {result}"
    return result.split("Session token:")[-1].strip()


@pytest.mark.asyncio
async def test_terminal_status_triggers_proxy_callback(dispatch_server):
    """DONE＋有 callback token → _notify_touka_proxy 恰被觸發一次（token/task/status）。

    變異刪 server.py:399-418 終態回呼段 → 零觸發、等待者空等到 TIMEOUT → 紅。
    """
    token = await _handshake(dispatch_server)
    out = await dispatch_server.dispatch_update_task(
        session_token=token, task_id="t1", status="DONE", test_log="3 passed"
    )
    assert "updated to DONE" in out
    assert _NOTIFIED == [("cb-tok-1", "t1", "DONE", "3 passed")]


@pytest.mark.asyncio
async def test_non_terminal_status_never_notifies(dispatch_server):
    """DOING（非終態）→ 不觸發回呼（陰性對照：終態條件真的把關）。"""
    token = await _handshake(dispatch_server)
    await dispatch_server.dispatch_update_task(
        session_token=token, task_id="t1", status="DOING"
    )
    assert _NOTIFIED == []


@pytest.mark.asyncio
async def test_notify_touka_proxy_posts_callback_endpoint(monkeypatch):
    """_notify_touka_proxy 本體：POST 至 /v1/internal/dispatch/callback、
    X-Callback-Token header、payload 三欄——URL/header/token 全釘死。"""
    captured: dict[str, Any] = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(client_kwargs=kwargs)

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, url: str, json: Any = None, headers: Any = None,
                       timeout: Any = None) -> _FakeResp:
            captured.update(url=url, json=json, headers=headers)
            return _FakeResp()

    monkeypatch.setattr(ds.httpx, "AsyncClient", _FakeClient)

    await ds._notify_touka_proxy("tok-x", "t9", "FAILED", "boom")

    assert captured["client_kwargs"] == {"follow_redirects": False}  # 禁令 22
    assert captured["url"].endswith("/v1/internal/dispatch/callback")
    assert captured["headers"] == {"X-Callback-Token": "tok-x"}
    assert captured["json"] == {
        "task_id": "t9",
        "status": "FAILED",
        "result_summary": "boom",
    }
