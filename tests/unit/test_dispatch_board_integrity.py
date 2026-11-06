"""DA — ToukaDispatch board-integrity on the WRITE path (#13/#14, ADR-0064 A3).

`dispatch_get_board` rejects an external session whose `bound_mission_id` != the
active mission, but `dispatch_update_task` had no such check — so a stale external
session (which survives a mission replacement because `clear_mission_tokens` never
clears `_sessions`) could mutate a replaced mission's FSM. The fix mirrors the
`get_board` consistency gate onto the write path.

Mirrors the env-before-import pattern of test_dispatch_mission_token.py.
"""

from __future__ import annotations

import pytest


class TestUpdateTaskBoardIntegrity:
    @pytest.mark.asyncio
    async def test_update_task_rejects_stale_external_session(self, zone_data_root, monkeypatch):
        """An external session bound to a replaced mission must NOT write (the fix)."""
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")
        from memory_server import config_utils
        from ToukaDispatch import server as ds

        config_utils._reset_resource_proxy_cache()

        # an internal session creates the CURRENT mission
        hs = await ds.dispatch_handshake(dispatch_token="internal-secret", caller_name="Alice")
        internal = hs.split("Session token:")[-1].strip()
        await ds.dispatch_initialize_mission(
            session_token=internal,
            goal="Current mission",
            tasks=[{"id": "t1", "description": "do"}],
        )

        # a STALE external session bound to a different (replaced) mission id
        stale = ds._session_mgr.create_external_session(
            "OLD-MISSION-XYZ", {"name": "ExternalAI"}
        )
        result = await ds.dispatch_update_task(
            session_token=stale, task_id="t1", status="DOING"
        )
        assert (
            "no longer active" in result or "changed since your session" in result
        ), result

    @pytest.mark.asyncio
    async def test_update_task_allows_external_session_on_current_mission(
        self, zone_data_root, monkeypatch
    ):
        """An external session bound to the CURRENT mission passes the gate."""
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")
        from memory_server import config_utils
        from ToukaDispatch import server as ds

        config_utils._reset_resource_proxy_cache()

        hs = await ds.dispatch_handshake(dispatch_token="internal-secret", caller_name="Alice")
        internal = hs.split("Session token:")[-1].strip()
        await ds.dispatch_initialize_mission(
            session_token=internal,
            goal="Current mission",
            tasks=[{"id": "t1", "description": "do"}],
        )

        mission = await ds.MissionManager().get_current_mission(ds._get_dispatch_identity())
        bound = ds._session_mgr.create_external_session(
            mission.mission_id, {"name": "ExternalAI"}
        )
        result = await ds.dispatch_update_task(
            session_token=bound, task_id="t1", status="DOING"
        )
        # the board-integrity gate must NOT trip for the current mission
        assert "no longer active" not in result, result

    @pytest.mark.asyncio
    async def test_update_task_internal_session_not_gated(self, zone_data_root, monkeypatch):
        """Internal (non-external) sessions are not subject to the gate."""
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")
        from memory_server import config_utils
        from ToukaDispatch import server as ds

        config_utils._reset_resource_proxy_cache()

        hs = await ds.dispatch_handshake(dispatch_token="internal-secret", caller_name="Alice")
        internal = hs.split("Session token:")[-1].strip()
        await ds.dispatch_initialize_mission(
            session_token=internal,
            goal="Current mission",
            tasks=[{"id": "t1", "description": "do"}],
        )
        result = await ds.dispatch_update_task(
            session_token=internal, task_id="t1", status="DOING"
        )
        assert "no longer active" not in result, result
