"""TDD: ToukaDispatch 雙向回呼通道整合測試（ADR-0051）.

驗證：
  1. /v1/internal/dispatch/callback 拒絕非 localhost 來源（403）
  2. /v1/internal/dispatch/callback 拒絕未注冊 token（401）
  3. 完整回呼流程：token 注冊 → callback POST → 等待方解除阻擋（roundtrip）
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import Header
from fastapi.testclient import TestClient
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.routers.dispatch_callback import verify_dispatch_callback
from src.services.dispatch_callback_store import (
    _reset_callback_store,
    get_callback_store,
)


@pytest.fixture(autouse=True)
def reset_callback_store_between_tests():
    """每個測試前後重置 callback store，確保 token 不洩漏。"""
    _reset_callback_store()
    yield
    _reset_callback_store()


class TestDispatchCallbackEndpointSecurity:
    """安全邊界：非授權呼叫應被阻擋。"""

    def test_callback_endpoint_rejects_external_origin(self):
        """TestClient 預設 host='testclient'（非 localhost），應回傳 403。

        Guard #22: 內部端點必須限定 localhost 來源。
        """
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/internal/dispatch/callback",
                headers={"X-Callback-Token": "any-token"},
                json={"task_id": "t1", "status": "DONE", "result_summary": ""},
            )
        assert resp.status_code == 403, (
            f"非 localhost 來源應被拒絕（403），實際狀態碼：{resp.status_code}"
        )

    def test_callback_endpoint_rejects_invalid_token(self):
        """localhost 來源但 token 未注冊，應回傳 401。"""

        # 僅跳過 IP 檢查，保留 token 驗證邏輯
        async def ip_bypassed_verifier(
            x_callback_token: str = Header(alias="X-Callback-Token"),  # noqa: B008
        ) -> str:
            if not get_callback_store().is_registered(x_callback_token):
                raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)
            return x_callback_token

        app.dependency_overrides[verify_dispatch_callback] = ip_bypassed_verifier
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post(
                    "/v1/internal/dispatch/callback",
                    headers={"X-Callback-Token": "not-registered-token"},
                    json={"task_id": "t1", "status": "DONE", "result_summary": ""},
                )
        finally:
            app.dependency_overrides.pop(verify_dispatch_callback, None)

        assert resp.status_code == 401, (
            f"未注冊 token 應被拒絕（401），實際狀態碼：{resp.status_code}"
        )


class TestDispatchBidirectionalRoundtrip:
    """完整雙向回呼流程測試。"""

    def test_full_roundtrip(self):
        """Alice 注冊 token → 外部 AI 觸發 callback → 等待方解除阻擋。

        流程：
          1. 在 callback store 中注冊 token
          2. 模擬 ToukaDispatch POST /v1/internal/dispatch/callback
          3. 確認 deliver_result 成功交付
          4. 確認 wait_for_result 取得正確結果
        """
        # 準備 token
        store = get_callback_store()
        token = store.generate_token()
        store.register(token)

        # 跳過 IP 檢查（模擬 ToukaDispatch → ToukaProxy 在 Docker 內部網路的呼叫）
        async def ip_bypassed_verifier(
            x_callback_token: str = Header(alias="X-Callback-Token"),  # noqa: B008
        ) -> str:
            if not store.is_registered(x_callback_token):
                raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)
            return x_callback_token

        app.dependency_overrides[verify_dispatch_callback] = ip_bypassed_verifier
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post(
                    "/v1/internal/dispatch/callback",
                    headers={"X-Callback-Token": token},
                    json={
                        "task_id": "t1",
                        "status": "DONE",
                        "result_summary": "All checks passed",
                    },
                )
        finally:
            app.dependency_overrides.pop(verify_dispatch_callback, None)

        assert resp.status_code == 200, (
            f"合法 callback 應成功（200），實際：{resp.status_code} {resp.text}"
        )
        assert resp.json().get("status") == "delivered", (
            f"回應應含 status=delivered，實際：{resp.json()}"
        )

        # 確認 token 交付後 wait_for_result 能正確取得（Event 已 set，立即回傳）
        async def _check():
            result = await store.wait_for_result(token, timeout=1.0)
            assert result["status"] == "DONE"
            assert result["task_id"] == "t1"
            assert result["result_summary"] == "All checks passed"

        asyncio.run(_check())
