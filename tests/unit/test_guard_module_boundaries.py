"""Module-boundary guard keeps full checks without creating an import graph cache."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("returncode", [0, 1])
def test_import_linter_disables_only_cache_and_preserves_domain_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    returncode: int,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "module_boundaries_under_test", ROOT / "scripts/guard_module_boundaries.py"
    )
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=returncode, stdout="full contract result", stderr="tool diagnostic"
        )

    monkeypatch.setattr(guard, "subprocess", SimpleNamespace(run=fake_run))
    with pytest.raises(SystemExit) as raised:
        guard.main()

    assert raised.value.code == returncode
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["lint-imports", "--config", ".importlinter", "--no-cache"]
    assert kwargs["cwd"] == str(ROOT)
    assert kwargs["env"]["PYTHONPATH"] == os.pathsep.join(
        [str(ROOT / "ToukaProxy"), str(ROOT / "ToukaMemory/src")]
    )
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "ignore"
    captured = capsys.readouterr()
    assert "full contract result" in captured.out
    assert "tool diagnostic" in captured.out
