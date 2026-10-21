"""H-fix: project_audit.timestamp must be stored in MILLISECONDS, matching every
other updatedAt/timestamp in the system (save_project, upsert_session, ProjectModel
all use get_current_epoch()*1000). add_project_event stored int(get_current_epoch())
— SECONDS — making the audit timestamp 1000x smaller than its siblings (latent: it is
not rendered yet, but any future consumer comparing it would show 1970-era times).
"""

from __future__ import annotations

import pytest
from src.models.identity import AgentIdentity
from src.utils.async_db import add_project_event, get_async_db, init_db_async


@pytest.mark.asyncio
async def test_add_project_event_stores_milliseconds(monkeypatch):
    await init_db_async()
    monkeypatch.setattr("src.utils.async_db.get_current_epoch", lambda: 1_700_000_000.0)
    identity = AgentIdentity.for_role(
        agent_id="agentH", role="admin", is_human=True, capabilities=[]
    )

    await add_project_event("p_h", identity, "write", "wrote foo.py")

    async with get_async_db() as db, db.execute(
        "SELECT timestamp FROM project_audit WHERE project_id = ?", ("p_h",)
    ) as cur:
        row = await cur.fetchone()

    assert row is not None
    assert row["timestamp"] == 1_700_000_000_000  # seconds * 1000 = milliseconds
