"""MCP 的程序內帳本必須在測試邊界清空。"""

from types import SimpleNamespace

from src.services import mcp_isolation_store
from src.services.isolation import evolution_command
from src.services.isolation.mcp_launch import McpLaunchInfo, mcp_launch_book

from tests import conftest


def test_mcp_state_is_reset_before_and_after_each_test(tmp_path):
    def dirty():
        mcp_isolation_store._SEEN.add(tmp_path)
        mcp_isolation_store._UNREADABLE.add(tmp_path)
        evolution_command._skill_locks["probe"] = object()
        mcp_launch_book.record("probe", McpLaunchInfo("self", "agent", "ok", "launched", "none"))

    def clean():
        assert not mcp_isolation_store._SEEN
        assert not mcp_isolation_store._UNREADABLE
        assert not evolution_command._skill_locks
        assert mcp_launch_book.get("probe") is None

    fixture = conftest._isolation_test_doubles.__wrapped__(
        SimpleNamespace(fixturenames=[])
    )
    dirty()
    try:
        next(fixture)
        clean()
        dirty()
    finally:
        fixture.close()
    clean()
