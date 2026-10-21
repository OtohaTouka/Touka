"""P8-A hermetic mock authorization-server integration oracle."""

from __future__ import annotations

import pytest

from tests.unit.test_mcp_oauth_full_flow import (
    test_full_authorization_callback_status_and_bearer_flow_is_secret_safe as _run_flow,
)


@pytest.mark.asyncio
async def test_mock_as_authorization_code_flow(tmp_path):
    await _run_flow(tmp_path)
