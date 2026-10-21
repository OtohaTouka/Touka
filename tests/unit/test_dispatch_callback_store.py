"""TDD: DispatchCallbackStore 單元測試（ADR-0051 雙向回呼通道）.

驗證：
  1. generate_token 每次回傳唯一值
  2. register 後 is_registered 為 True
  3. wait_for_result 在 timeout 內無交付時回傳 TIMEOUT
  4. deliver_result 後 wait_for_result 立即解除阻擋並取得結果
  5. deliver_result 後 token 仍在（消耗後才移除）
  6. deliver 未知 token 回傳 False
  7. wait_for_result 完成後 token 自動移除
  8. wait 未知 token 回傳 UNKNOWN_TOKEN
"""

from __future__ import annotations

import asyncio

import pytest
from src.services.dispatch_callback_store import (
    DispatchCallbackStore,
    _reset_callback_store,
)


class TestDispatchCallbackStore:
    """DispatchCallbackStore 的完整單元測試。"""

    def setup_method(self):
        """每個測試前重置 singleton，確保隔離。"""
        _reset_callback_store()

    def test_generate_token_is_unique(self):
        """generate_token 每次呼叫應回傳不同的 token。"""
        store = DispatchCallbackStore()
        t1 = store.generate_token()
        t2 = store.generate_token()
        assert t1 != t2, "generate_token 必須回傳唯一 token"
        assert len(t1) >= 32, "token 應有足夠長度（≥32 字元）"

    def test_register_marks_token_as_pending(self):
        """register 後 is_registered 應回傳 True。"""
        store = DispatchCallbackStore()
        token = store.generate_token()
        store.register(token)
        assert store.is_registered(token)

    @pytest.mark.asyncio
    async def test_await_times_out_when_no_delivery(self):
        """在 timeout 內無 deliver_result 時，wait_for_result 應回傳 status=TIMEOUT。"""
        store = DispatchCallbackStore()
        token = store.generate_token()
        store.register(token)
        result = await store.wait_for_result(token, timeout=0.05)
        assert result["status"] == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_deliver_result_unblocks_waiter(self):
        """deliver_result 後，wait_for_result 應立即解除阻擋並回傳正確結果。"""
        store = DispatchCallbackStore()
        token = store.generate_token()
        store.register(token)

        async def deliver():
            await asyncio.sleep(0.05)
            store.deliver_result(token, {"status": "DONE", "task_id": "t1"})

        asyncio.create_task(deliver())
        result = await store.wait_for_result(token, timeout=1.0)
        assert result["status"] == "DONE"
        assert result["task_id"] == "t1"

    def test_token_still_registered_after_delivery(self):
        """deliver_result 後 token 仍應在 pending 中（wait 完成後才移除）。"""
        store = DispatchCallbackStore()
        token = store.generate_token()
        store.register(token)
        store.deliver_result(token, {"status": "DONE"})
        # deliver 後尚未 wait，token 應仍在
        assert store.is_registered(token)

    def test_deliver_unknown_token_returns_false(self):
        """deliver 未知 token 應回傳 False（不可 KeyError 崩潰）。"""
        store = DispatchCallbackStore()
        result = store.deliver_result("nonexistent-token", {})
        assert result is False

    @pytest.mark.asyncio
    async def test_token_removed_after_wait_completes(self):
        """wait_for_result 完成後，token 應自動從 pending 中移除。"""
        store = DispatchCallbackStore()
        token = store.generate_token()
        store.register(token)
        store.deliver_result(token, {"status": "DONE"})
        await store.wait_for_result(token, timeout=1.0)
        assert not store.is_registered(token), "wait 完成後 token 必須移除"

    @pytest.mark.asyncio
    async def test_unknown_token_wait_returns_unknown_status(self):
        """wait 未知 token 應立即回傳 status=UNKNOWN_TOKEN。"""
        store = DispatchCallbackStore()
        result = await store.wait_for_result("no-such-token", timeout=0.05)
        assert result["status"] == "UNKNOWN_TOKEN"
