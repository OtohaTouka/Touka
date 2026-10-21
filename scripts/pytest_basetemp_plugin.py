"""Pytest startup policy for explicit ``--basetemp`` values."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_residue_safety import BasetempPolicyError, validate_explicit_basetemp


def pytest_configure(config: pytest.Config) -> None:
    """Reject explicit repository-local or unverifiable basetemp paths."""
    basetemp = config.option.basetemp
    if basetemp is None:
        return

    try:
        validate_explicit_basetemp(
            basetemp,
            repo_root=Path(config.rootpath),
            cwd=Path.cwd(),
        )
    except BasetempPolicyError as exc:
        raise pytest.UsageError(str(exc)) from exc
