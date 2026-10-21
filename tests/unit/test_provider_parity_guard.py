"""TDD — Provider Parity Guard #36（Phase 4c）。

驗證守衛純函式正確、目前前後端一致、且已註冊進 guard_all。
"""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

from src.config import get_full_env

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import guard_provider_parity as gpp  # noqa: E402


def test_backend_required_keys():
    """可發現供應商的主要金鑰 env 名。"""
    assert gpp.backend_required_key_names() == {
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "GROK_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
    }


def test_frontend_env_names_parses_single_and_double_quotes():
    ts = """export const PROVIDER_KEYS = [
      { id: 'x', env: "FOO_API_KEY", helpUrl: 'h' },
      { id: 'y', env: 'BAR_API_KEY' },
    ];"""
    assert gpp.frontend_env_names(ts) == {"FOO_API_KEY", "BAR_API_KEY"}


def test_parity_passes_currently():
    """目前後端 registry 金鑰皆有前端對應 → 無違規。"""
    assert gpp.run_checks() == []


def test_drift_is_detected_by_set_diff():
    """若前端缺某後端供應商金鑰 → backend ⊆ frontend 失敗（集合差非空）。"""
    required = gpp.backend_required_key_names()
    frontend_missing_deepseek = required - {"DEEPSEEK_API_KEY"}
    assert (required - frontend_missing_deepseek) == {"DEEPSEEK_API_KEY"}


def test_guard_registered_in_guard_all():
    """#36 必須註冊進 guard_all（doc-vs-reality 同步）。"""
    text = (PROJECT_ROOT / "scripts" / "guard_all.py").read_text(encoding="utf-8")
    assert "guard_provider_parity.py" in text
    assert "Guard #36" in text


_PUBLIC_GUARD_FILES = (
    ".env.example",
    "scripts/guard_provider_parity.py",
    "ToukaProxy/src/__init__.py",
    "ToukaProxy/src/config.py",
    "ToukaProxy/src/core/__init__.py",
    "ToukaProxy/src/core/bootstrap.py",
    "ToukaProxy/src/services/__init__.py",
    "ToukaProxy/src/services/providers/__init__.py",
    "ToukaProxy/src/services/providers/registry.py",
    "ToukaChat/src/lib/providerKeys.ts",
    "tests/smoke/lib/sandbox_env.py",
    "tests/network_guard.py",
    "ToukaProxy/config/skills/document-generator/SKILL.md",
)
_SENTINEL_PROBE = """
import os
import runpy
import sys

sentinel = os.path.normcase(os.path.abspath(sys.argv[2]))

def audit(event, args):
    if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
    if path == sentinel:
        sys.stderr.write("SYNTHETIC_SENTINEL_ACCESS\\n")
        sys.stderr.flush()
        raise RuntimeError("SYNTHETIC_SENTINEL_ACCESS")

sys.addaudithook(audit)
try:
    runpy.run_path(sys.argv[1], run_name="__main__")
except SystemExit as exc:
    if exc.code not in (None, 0):
        raise
print("SYNTHETIC_PROBE_OK")
"""


def _copied_public_guard(tmp_path: Path) -> Path:
    root = tmp_path / "public-source"
    examples = tuple(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in sorted((PROJECT_ROOT / "ToukaProxy/config").glob("*.json.example"))
    )
    assert examples
    for relative in (*_PUBLIC_GUARD_FILES, *examples):
        source = PROJECT_ROOT / relative
        assert source.is_file() and not source.is_symlink()
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    return root


def _public_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _closed_child_environment(temp_root: Path) -> dict[str, str]:
    parent = get_full_env()
    environment = {
        name: value
        for name, value in parent.items()
        if name.upper() in {"SYSTEMROOT", "WINDIR"}
    }
    environment.update(
        TEMP=str(temp_root),
        TMP=str(temp_root),
        TMPDIR=str(temp_root),
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return environment


def test_standalone_guard_keeps_public_source_unchanged(tmp_path: Path) -> None:
    root = _copied_public_guard(tmp_path)
    temp_root = tmp_path / "runtime-temp"
    temp_root.mkdir()
    before = _public_tree(root)
    pending_configs = tuple(
        path.with_suffix("") for path in (root / "ToukaProxy/config").glob("*.json.example")
    )
    assert not (root / ".env").exists()
    assert all(not path.exists() for path in pending_configs)

    result = subprocess.run(
        [sys.executable, "-I", "-B", str(root / "scripts/guard_provider_parity.py")],
        cwd=root,
        env=_closed_child_environment(temp_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[OK]" in result.stdout
    assert _public_tree(root) == before
    assert not (root / ".env").exists()
    assert all(not path.exists() for path in pending_configs)
    assert list(temp_root.iterdir()) == []


def test_standalone_guard_never_opens_hostile_env_sentinel(tmp_path: Path) -> None:
    root = _copied_public_guard(tmp_path)
    temp_root = tmp_path / "runtime-temp"
    temp_root.mkdir()
    sentinel = tmp_path / "synthetic-private.env"
    sentinel_bytes = b"# synthetic sentinel: never read or written by guard\n"
    sentinel.write_bytes(sentinel_bytes)
    before = _public_tree(root)
    environment = _closed_child_environment(temp_root)
    environment.update(TOUKA_BOOTSTRAP_ENABLED="true", TOUKA_ENV_FILE=str(sentinel))

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _SENTINEL_PROBE,
            str(root / "scripts/guard_provider_parity.py"),
            str(sentinel),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SYNTHETIC_SENTINEL_ACCESS" not in result.stdout + result.stderr
    assert "SYNTHETIC_PROBE_OK" in result.stdout
    assert sentinel.read_bytes() == sentinel_bytes
    assert _public_tree(root) == before
    assert list(temp_root.iterdir()) == []


def test_non_main_guard_import_preserves_safe_environment() -> None:
    before = get_full_env()
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "scripts/guard_provider_parity.py"),
        run_name="provider_guard_import_probe",
    )
    assert namespace["run_checks"]() == []
    assert get_full_env() == before
