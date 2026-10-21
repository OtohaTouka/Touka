"""The session fixture may seed only public examples, never private settings."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

CONFIG_NAMES = ("permissions.json", "auth_policies.json")


def _isolated_fixture(tmp_path: Path) -> tuple[Any, Path, Path]:
    """Execute the actual fixture body against an owned tree, without app imports."""
    source = Path(__file__).resolve().parents[1] / "conftest.py"
    function = next(
        node
        for node in ast.parse(source.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "global_test_sandbox"
    )
    function.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    config = tmp_path / "repo" / "ToukaProxy" / "config"
    config.mkdir(parents=True)
    session = tmp_path / "session"
    session.mkdir()
    for name in CONFIG_NAMES:
        (config / name).write_text("synthetic-private-sentinel", encoding="utf-8")
        (config / f"{name}.example").write_text("public-example", encoding="utf-8")
    namespace = {
        "Path": Path,
        "shutil": shutil,
        "permission_service": SimpleNamespace(reload_all=lambda: None),
        "SESSION_TMP_DATA_ROOT": str(session),
        "PROJECT_ROOT": tmp_path / "repo",
        "__file__": str(tmp_path / "repo" / "tests" / "conftest.py"),
    }
    exec(compile(module, "owned-session-fixture", "exec"), namespace)  # noqa: S102
    return namespace["global_test_sandbox"](), config, session


def test_session_fixture_uses_public_examples_even_when_private_settings_exist(
    tmp_path: Path,
) -> None:
    generator, _config, session = _isolated_fixture(tmp_path)
    next(generator)
    try:
        for name in CONFIG_NAMES:
            assert (session / "config" / name).read_text(encoding="utf-8") == "public-example"
    finally:
        generator.close()


@pytest.mark.parametrize("missing", CONFIG_NAMES)
def test_missing_public_example_fails_without_private_fallback(
    tmp_path: Path, missing: str
) -> None:
    generator, config, _session = _isolated_fixture(tmp_path)
    (config / f"{missing}.example").unlink()
    with pytest.raises(FileNotFoundError, match="example"):
        next(generator)
