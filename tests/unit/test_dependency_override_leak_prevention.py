"""TDD tests verifying that app.dependency_overrides are properly cleared by setup_test_environment.

Ensures that even if a test exits with an exception, dependency overrides do not leak.
"""
from __future__ import annotations

import contextlib

from src.main import app

from tests.conftest import setup_test_environment


def test_setup_test_environment_forces_override_cleanup(tmp_path) -> None:
    """Verifies that dependency overrides set during a test are cleared during teardown."""
    # Ensure starting with a clean slate
    app.dependency_overrides.clear()

    # 1. Simulate setup_test_environment fixture execution using __wrapped__
    # to bypass pytest restrictions.
    # 沙盒必須是 pytest tmp_path：字面相對路徑會以 cwd（repo root）解析，讓 fixture
    # 步驟 4 的單例重置把 repo 內目錄快取成技能根目錄並弄髒版控。
    dummy_sandbox = str(tmp_path)
    fixture_gen = setup_test_environment.__wrapped__(dummy_sandbox)

    # Move fixture to yield (setup phase)
    next(fixture_gen)

    # 2. Simulate test code running (which could mutate overrides or run assertions)
    app.dependency_overrides["dummy_dependency"] = lambda: "mocked_value"
    app.dependency_overrides["another_dependency"] = lambda: "another_mock"

    # 3. Simulate fixture teardown (pytest resumes generator after test execution)
    with contextlib.suppress(StopIteration):
        next(fixture_gen)

    # 4. Verify that everything set during the test was cleared
    assert len(app.dependency_overrides) == 0, (
        "app.dependency_overrides set during test must be completely cleared after test teardown."
    )
