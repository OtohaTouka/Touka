"""TDD tests verifying that app.dependency_overrides are restored to their pre-test state.

Ensures that we do not over-clear overrides belonging to upper-scope fixtures or clients.
"""
from __future__ import annotations

import contextlib

from src.main import app

from tests.conftest import setup_test_environment


def test_setup_test_environment_preserves_external_overrides(tmp_path) -> None:
    """Verifies that pre-existing overrides are preserved, while test overrides are cleared."""
    # Ensure starting with a clean slate
    app.dependency_overrides.clear()

    # 1. Simulate an upper-scope/external override setup (e.g., auth_client or module fixture)
    app.dependency_overrides["external_dep"] = lambda: "external_mock"

    # 2. Simulate setup_test_environment fixture execution
    # 沙盒必須是 pytest tmp_path（理由同 test_dependency_override_leak_prevention）。
    dummy_sandbox = str(tmp_path)
    fixture_gen = setup_test_environment.__wrapped__(dummy_sandbox)

    # Move fixture to yield (setup phase)
    next(fixture_gen)

    # 3. Simulate test code running and setting its own override
    app.dependency_overrides["test_dep"] = lambda: "test_mock"
    assert "external_dep" in app.dependency_overrides
    assert "test_dep" in app.dependency_overrides

    # 4. Simulate fixture teardown
    with contextlib.suppress(StopIteration):
        next(fixture_gen)

    # 5. Verify that only the test override was cleared, and the external override was preserved
    assert "test_dep" not in app.dependency_overrides, "Test override should be cleared."
    assert "external_dep" in app.dependency_overrides, "External override should be preserved."
    assert app.dependency_overrides["external_dep"]() == "external_mock"
    assert len(app.dependency_overrides) == 1
