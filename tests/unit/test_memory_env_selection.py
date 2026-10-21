"""Memory bootstrap respects the existing explicit environment source contract."""

from pathlib import Path
from unittest.mock import Mock, call

import pytest
from memory_server import config_utils


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize("memory_already_set", [False, True])
def test_explicit_environment_never_uses_implicit_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exists: bool,
    memory_already_set: bool,
) -> None:
    """A real owned explicit file loads; a missing explicit file never falls back."""
    selected = tmp_path / "selected env" / ".env"
    selected.parent.mkdir()
    if exists:
        selected.write_text("EMBEDDING_MODEL=owned-value\n", encoding="utf-8")
    monkeypatch.setenv("TOUKA_ENV_FILE", str(selected))
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    default_embedding = config_utils.get_embedding_model_name()
    if memory_already_set:
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path / "existing-memory"))
    else:
        monkeypatch.delenv("MEMORY_APP_DATA_DIR", raising=False)
    real_load = config_utils.load_dotenv
    calls: list[Path] = []

    def owned_load(*, dotenv_path: str | Path | None = None) -> bool:
        assert dotenv_path is not None, "implicit environment discovery attempted"
        path = Path(dotenv_path)
        assert path == selected
        calls.append(path)
        return real_load(dotenv_path=path)

    monkeypatch.setattr(config_utils, "load_dotenv", owned_load)
    # The RED command protects collection; only after the safe seam exists may
    # this test exercise python-dotenv against its exact owned source.
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    config_utils.bootstrap_env()
    assert calls == [selected]
    assert config_utils.get_embedding_model_name() == (
        "owned-value" if exists else default_embedding
    )


@pytest.mark.parametrize("explicit", [None, ""])
def test_absent_or_empty_explicit_keeps_existing_initial_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit: str | None
) -> None:
    """An absent/empty override preserves the Proxy-compatible default boundary."""
    if explicit is None:
        monkeypatch.delenv("TOUKA_ENV_FILE", raising=False)
    else:
        monkeypatch.setenv("TOUKA_ENV_FILE", explicit)
    monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
    loader = Mock(return_value=False)
    monkeypatch.setattr(config_utils, "load_dotenv", loader)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    config_utils.bootstrap_env()
    loader.assert_called_once_with()


def test_no_explicit_keeps_bounded_three_level_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Existing fallback examines at most three owned directories after discovery."""
    deepest = tmp_path / "one" / "two" / "three"
    deepest.mkdir(parents=True)
    candidates = [deepest, deepest.parent, deepest.parent.parent]
    for parent in candidates:
        (parent / ".env").write_text("# owned fallback fixture\n", encoding="utf-8")
    monkeypatch.delenv("TOUKA_ENV_FILE", raising=False)
    monkeypatch.delenv("MEMORY_APP_DATA_DIR", raising=False)
    loader = Mock(return_value=False)
    monkeypatch.setattr(config_utils, "load_dotenv", loader)
    monkeypatch.setattr(config_utils.Path, "cwd", lambda: deepest)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    config_utils.bootstrap_env()
    assert loader.call_args_list == [
        call(),
        *(call(dotenv_path=parent / ".env") for parent in candidates),
    ]
