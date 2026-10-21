"""G-fix: project_tools must not record a phantom audit event when an op FAILS.

The success check was `if "Success" in res` — a substring of the op's return
string. core_file_ops echoes user-controlled input into its ERROR messages, so a
FAILED op whose path/old_str contains the literal "Success" (e.g. replacing a
`print("Success")` line) still matched, recording a write/replace/delete that
never happened — corrupting the project audit trail (overview panel + handover).

Fix: anchor the check to `res.startswith("Success")` — every core_file_ops success
return starts with "Success:" and every error starts with "Error", so a user's
"Success" echoed inside an error string can no longer forge an audit event.
"""

from __future__ import annotations

import pytest
from src.models.identity import AgentIdentity
from src.tools import project_tools as pt

ADMIN = AgentIdentity.for_role(agent_id="admin-id", role="admin", is_human=True, capabilities=[])


def _patch_common(monkeypatch, tmp_path, core_result, events):
    async def _add_event(*args, **_kwargs):
        events.append(args)

    async def _get_path(_pid, _identity=None):
        return str(tmp_path)

    async def _replace(*_args, **_kwargs):
        return core_result

    monkeypatch.setattr("src.tools.project_tools.add_project_event", _add_event)
    monkeypatch.setattr("src.tools.project_tools.resource_manager.get_project_path", _get_path)
    monkeypatch.setattr("src.tools.project_tools.core_str_replace", _replace)


@pytest.mark.asyncio
async def test_failed_op_echoing_success_records_no_audit(monkeypatch, tmp_path):
    events: list = []
    # A core_str_replace FAILURE that echoes the user's literal "Success".
    _patch_common(
        monkeypatch,
        tmp_path,
        "Error: String 'print(\"Success\")' not found in foo.py.",
        events,
    )
    res = await pt.project_tools(
        "str_replace",
        path="foo.py",
        old_str='print("Success")',
        new_str="x",
        project_id="p1",
        identity=ADMIN,
    )
    assert res.startswith("Error")
    assert events == []  # no phantom audit event for a failed op


@pytest.mark.asyncio
async def test_successful_op_records_audit(monkeypatch, tmp_path):
    events: list = []
    _patch_common(monkeypatch, tmp_path, "Success: Replaced 'x' in foo.py", events)
    res = await pt.project_tools(
        "str_replace",
        path="foo.py",
        old_str="x",
        new_str="y",
        project_id="p1",
        identity=ADMIN,
    )
    assert res.startswith("Success")
    assert len(events) == 1  # a real success still records exactly one audit event
