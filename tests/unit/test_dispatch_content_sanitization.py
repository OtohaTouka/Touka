"""DA #11: external-AI dispatch text must pass the H3 content guard before it
reaches the supervising Touka agent's LLM context.

The board/callback paths previously skipped `sanitize_memory_content` (which only
ToukaMemory's save_memory used), so an external AI's test_log/blocker_detail/
result_summary entered the agent context unsanitised. The fix wires H3 (truncate
to 8000 + detect/log prompt-injection) into both `dispatch_update_task` (the write
choke point) and `dispatch_callback` (defense-in-depth). These tests verify the
observable H3 effect — length truncation — at both boundaries.

The ToukaDispatch test uses the DISPATCH_TOKEN-then-deferred-import pattern, so
this file is on lint_ssot's WHITELIST_FILES like the sibling dispatch tests.
"""

from __future__ import annotations

import pytest
from src.routers.dispatch_callback import (
    DispatchCallbackPayload,
    receive_dispatch_callback,
)
from src.services.dispatch_callback_store import _reset_callback_store, get_callback_store

LONG = "A" * 9000  # > content_guard.MAX_CONTENT_LENGTH (8000)


class TestDispatchCallbackSanitization:
    @pytest.mark.asyncio
    async def test_callback_truncates_long_result_summary(self):
        """The callback must sanitise result_summary before delivering it."""
        _reset_callback_store()
        store = get_callback_store()
        store.register("tok-da11")

        payload = DispatchCallbackPayload(task_id="t1", status="DONE", result_summary=LONG)
        await receive_dispatch_callback(payload=payload, callback_token="tok-da11")

        result = await store.wait_for_result("tok-da11", timeout=0.5)
        assert len(result["result_summary"]) <= 8000


class TestDispatchUpdateTaskSanitization:
    @pytest.mark.asyncio
    async def test_update_task_truncates_long_test_log(self, zone_data_root, monkeypatch):
        """dispatch_update_task must sanitise test_log before it is stored/rendered."""
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")
        from memory_server import config_utils
        from ToukaDispatch import server as ds

        config_utils._reset_resource_proxy_cache()

        hs = await ds.dispatch_handshake(dispatch_token="internal-secret", caller_name="Alice")
        internal = hs.split("Session token:")[-1].strip()
        await ds.dispatch_initialize_mission(
            session_token=internal,
            goal="m",
            tasks=[{"id": "t1", "description": "do"}],
        )

        await ds.dispatch_update_task(
            session_token=internal, task_id="t1", status="DOING", test_log=LONG
        )

        mission = await ds.MissionManager().get_current_mission(ds._get_dispatch_identity())
        task = next(t for t in mission.tasks if t.id == "t1")
        assert task.test_log is not None
        assert len(task.test_log) <= 8000
