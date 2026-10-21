"""Regression tests for ``app.state.context`` lifecycle isolation."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from src.main import app

from tests.conftest import setup_test_environment

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


_MISSING = object()


def _finish_fixture(fixture_gen: Generator[None, None, None]) -> None:
    with contextlib.suppress(StopIteration):
        next(fixture_gen)


def _restore_outer_context(original_context: object) -> None:
    if original_context is _MISSING:
        if hasattr(app.state, "context"):
            del app.state.context
    else:
        app.state.context = original_context


def test_teardown_restores_missing_app_context(tmp_path: Path) -> None:
    """A context created during a test must not leak into the next test."""
    original_context = getattr(app.state, "context", _MISSING)
    if original_context is not _MISSING:
        del app.state.context

    fixture_gen = setup_test_environment.__wrapped__(str(tmp_path))
    try:
        next(fixture_gen)
        app.state.context = {"protocol_manager": object()}

        _finish_fixture(fixture_gen)

        assert not hasattr(app.state, "context")
    finally:
        _finish_fixture(fixture_gen)
        _restore_outer_context(original_context)


def test_teardown_restores_existing_app_context(tmp_path: Path) -> None:
    """An existing context must be restored by value after a test mutates it."""
    original_context = getattr(app.state, "context", _MISSING)
    baseline_manager = object()
    app.state.context = {
        "protocol_manager": baseline_manager,
        "tool_registry": {},
    }

    fixture_gen = setup_test_environment.__wrapped__(str(tmp_path))
    try:
        next(fixture_gen)
        app.state.context["protocol_manager"] = object()
        app.state.context["leaked"] = True

        _finish_fixture(fixture_gen)

        assert app.state.context["protocol_manager"] is baseline_manager
        assert "leaked" not in app.state.context
    finally:
        _finish_fixture(fixture_gen)
        _restore_outer_context(original_context)
