"""Guard #30（Environment Parity，ADR-0117 uv workspace 版）單元測試。

以 importlib 載入守衛，餵 in-memory TOML 給純掃描函式；並驗證現況 repo 全綠。
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "guard_environment_parity.py"


def _load():
    spec = importlib.util.spec_from_file_location("guard_env_parity_under_test", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


g = _load()


# ── Check 1：pyproject ↔ uv.lock 逐項比對 ────────────────────────────────────


def test_parse_req_plain_and_extras_and_marker():
    assert g.parse_req("fastapi~=0.133.0") == ("fastapi", "~=0.133.0")
    assert g.parse_req("pydantic[email]~=2.12.5") == ("pydantic", "~=2.12.5")
    assert g.parse_req("discord.py==2.3.2") == ("discord-py", "==2.3.2")
    assert g.parse_req('tzdata==2026.2 ; sys_platform == "win32"') == ("tzdata", "==2026.2")


_PYPROJECT = """\
[project]
name = "demo"
dependencies = ["fastapi~=0.133.0", "pydantic[email]~=2.12.5"]

[dependency-groups]
dev = ["pytest~=9.1.1"]
"""

_LOCK_OK = """\
[[package]]
name = "demo"
version = "0.1.0"
source = { virtual = "Demo" }

[package.metadata]
requires-dist = [
    { name = "fastapi", specifier = "~=0.133.0" },
    { name = "pydantic", specifier = "~=2.12.5", extras = ["email"] },
]

[[package]]
name = "touka"
version = "0.1.0"
source = { virtual = "." }

[package.metadata.requires-dev]
dev = [{ name = "pytest", specifier = "~=9.1.1" }]
"""


def test_pyproject_lock_pairs_match():
    doc = tomllib.loads(_PYPROJECT)
    lock = tomllib.loads(_LOCK_OK)
    declared = g.pyproject_req_pairs(doc)
    locked = g.lock_member_pairs(lock, "demo")
    assert locked is not None
    assert g.diff_pairs(declared, locked) == []


def test_installable_editable_workspace_member_lock_pairs_match():
    """Installable workspace members use ``source.editable`` instead of ``virtual``."""
    doc = tomllib.loads(_PYPROJECT)
    lock = tomllib.loads(_LOCK_OK.replace('virtual = "Demo"', 'editable = "Demo"'))
    locked = g.lock_member_pairs(lock, "demo")
    assert locked is not None
    assert g.diff_pairs(g.pyproject_req_pairs(doc), locked) == []


def test_optional_dependency_group_participates_in_lock_freshness():
    doc = tomllib.loads(
        _PYPROJECT + '\n[project.optional-dependencies]\nazure = ["azure-functions==1.25.0"]\n'
    )
    lock = tomllib.loads(
        _LOCK_OK.replace(
            '    { name = "pydantic", specifier = "~=2.12.5", extras = ["email"] },',
            '    { name = "pydantic", specifier = "~=2.12.5", extras = ["email"] },\n'
            '    { name = "azure-functions", marker = "extra == \'azure\'", '
            'specifier = "==1.25.0" },',
        )
    )
    locked = g.lock_member_pairs(lock, "demo")
    assert locked is not None
    assert g.diff_pairs(g.pyproject_req_pairs(doc), locked) == []


def test_lock_stale_when_pyproject_edited():
    """改了 pyproject 規格（~=2.12.5 → ~=2.13.0）而未重鎖 → 必須被抓出。"""
    doc = tomllib.loads(_PYPROJECT.replace("~=2.12.5", "~=2.13.0"))
    lock = tomllib.loads(_LOCK_OK)
    diffs = g.diff_pairs(g.pyproject_req_pairs(doc), g.lock_member_pairs(lock, "demo"))
    assert diffs, "版本規格漂移必須被偵測"
    assert any("pydantic" in d for d in diffs)


def test_lock_stale_when_dep_added():
    doc = tomllib.loads(
        _PYPROJECT.replace('"fastapi~=0.133.0"', '"fastapi~=0.133.0", "httpx~=0.28.1"')
    )
    lock = tomllib.loads(_LOCK_OK)
    diffs = g.diff_pairs(g.pyproject_req_pairs(doc), g.lock_member_pairs(lock, "demo"))
    assert any("httpx" in d and "未記錄" in d for d in diffs)


def test_lock_stale_when_dep_removed():
    doc = tomllib.loads(_PYPROJECT.replace('"fastapi~=0.133.0", ', ""))
    lock = tomllib.loads(_LOCK_OK)
    diffs = g.diff_pairs(g.pyproject_req_pairs(doc), g.lock_member_pairs(lock, "demo"))
    assert any("fastapi" in d and "已無此宣告" in d for d in diffs)


def test_lock_member_missing_returns_none():
    lock = tomllib.loads(_LOCK_OK)
    assert g.lock_member_pairs(lock, "no-such-member") is None


def test_root_dev_group_pairs():
    doc = tomllib.loads(_PYPROJECT)
    lock = tomllib.loads(_LOCK_OK)
    assert g.diff_pairs(g.pyproject_dev_pairs(doc), g.lock_root_dev_pairs(lock)) == []


# ── Check 2：Python 版本一致（沿用純函式）────────────────────────────────────


def test_py_from_target():
    assert g.py_from_target("py312") == "3.12"
    assert g.py_from_target("py314") == "3.14"
    assert g.py_from_target("nonsense") is None


def test_py_from_dockerfile():
    assert g.py_from_dockerfile("FROM python:3.12-slim\nRUN x") == "3.12"
    assert g.py_from_dockerfile("FROM node:20\n") is None


def test_py_from_dockerfile_multistage_first_from():
    text = "FROM python:3.12-slim AS deps\nFROM python:3.12-slim\n"
    assert g.py_from_dockerfile(text) == "3.12"


@pytest.mark.parametrize(
    "command",
    [
        "RUN apk add --no-cache python-3.12=3.12.14-r6",
        "RUN set -eux; apk upgrade --no-cache; apk add --no-cache python-3.12=3.12.14-r6",
        "RUN apk add --no-cache \\\n    python-3.12=3.12.14-r6 \\\n    libstdc++=16.2.0-r1",
        "RUN apk add --no-cache \\\n# Docker continuation comment\n    python-3.12=3.12.14-r6",
        "RUN apk add python-3.12=3.12.14-r6 && python3.12 --version",
    ],
)
def test_wolfi_python_minor_comes_from_actual_pinned_apk_add(command):
    text = "FROM cgr.dev/chainguard/wolfi-base@sha256:" + "a" * 64 + "\n" + command
    assert g.py_from_dockerfile(text) == "3.12"


@pytest.mark.parametrize(
    "text",
    [
        "# RUN apk add python-3.12=3.12.14-r6",
        "RUN echo 'apk add python-3.12=3.12.14-r6'",
        "ENV PACKAGE=python-3.12=3.12.14-r6",
        "RUN apk add openssl # python-3.12=3.12.14-r6",
        "RUN apk add python-3.12",
        "RUN apk add python-3.12=3.13.14-r6",
        "RUN apk add python-3.12=3.12.14",
        "RUN apk add python-3.12=3.12.14-r6 python-3.13=3.13.7-r1",
        "FROM python:3.12-slim\nFROM python:3.13-slim",
        "FROM python:3.12-slim\nRUN apk add python-3.13=3.13.7-r1",
        "FROM python@sha256:" + "a" * 64,
    ],
)
def test_docker_python_minor_rejects_unbound_or_ambiguous_declarations(text):
    assert g.py_from_dockerfile(text) is None


def test_escaped_shell_separator_does_not_turn_echo_into_apk_add():
    text = "RUN echo foo\\; apk add python-3.12=3.12.14-r6"
    assert g.py_from_dockerfile(text) is None


def _parity_root(tmp_path, monkeypatch, dockerfile):
    monkeypatch.setattr(g, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(g, "_MEMBERS", {})
    monkeypatch.setattr(g, "_DOCKER_PY", ("Service",))
    (tmp_path / ".python-version").write_text("3.12\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="touka"\nrequires-python="==3.12.*"\n[dependency-groups]\ndev=[]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="touka"\nsource={virtual="."}\n'
        "[package.metadata.requires-dev]\ndev=[]\n",
        encoding="utf-8",
    )
    if dockerfile is not None:
        (tmp_path / "Service").mkdir()
        (tmp_path / "Service" / "Dockerfile").write_text(dockerfile, encoding="utf-8")


@pytest.mark.parametrize(
    "dockerfile",
    [
        None,
        "FROM unknown-base\n",
        "FROM wolfi\n# RUN apk add python-3.12=3.12.14-r6\n",
        "FROM wolfi\nRUN apk add python-3.12\n",
        "FROM wolfi\nRUN apk add python-3.13=3.13.7-r1\n",
    ],
)
def test_run_all_cannot_silently_skip_missing_or_unknown_docker_python(
    tmp_path, monkeypatch, dockerfile
):
    _parity_root(tmp_path, monkeypatch, dockerfile)
    errors = g.run_all()
    assert errors and all(error.startswith("[PYVER]") for error in errors)
    assert any("Service/Dockerfile" in error for error in errors)


def test_run_all_admits_the_pinned_wolfi_python_minor(tmp_path, monkeypatch):
    _parity_root(tmp_path, monkeypatch, "FROM wolfi\nRUN apk add python-3.12=3.12.14-r6\n")
    assert g.run_all() == []


def test_py_from_requires_and_ruff_and_norm():
    assert g.py_from_requires('requires-python = "==3.12.*"') == "3.12"
    assert g.py_from_ruff('target-version = "py312"') == "3.12"
    assert g.norm_pyver("3.12.12") == "3.12"


# ── Check 3：釘版政策 ─────────────────────────────────────────────────────────


def test_scan_ge_flags_only_ge_lines():
    reqs = ["fastapi~=0.133.0", "torch>=2.0.0", "pydantic==2.12.5"]
    assert g.scan_ge_reqs(reqs) == ["torch>=2.0.0"]


# ── 整合：現況 repo 必須全綠 ──────────────────────────────────────────────────


def test_repo_environment_parity_clean():
    assert g.run_all() == [], "現況 repo 的 pyproject/uv.lock/Python 版本/釘版必須一致"


# ── Check 4：venv-parity advisory（唯讀、純比對）──────────────────────────────

_LOCKMAP = {"fastapi": {"0.133.1"}, "torch": {"2.12.1", "2.12.1+cpu"}}


def test_venv_parity_mismatch_flagged():
    drift = g.venv_parity_drift("fastapi==0.999.0\n", _LOCKMAP)
    assert len(drift) == 1 and "fastapi" in drift[0]


def test_venv_parity_multi_version_lock_accepts_any():
    """同名多版本（torch/+cpu resolution markers）——任一鎖定版本皆視為一致。"""
    assert g.venv_parity_drift("torch==2.12.1+cpu\n", _LOCKMAP) == []
    assert g.venv_parity_drift("torch==2.12.1\n", _LOCKMAP) == []
    assert g.venv_parity_drift("torch==2.11.0\n", _LOCKMAP) != []


def test_venv_parity_ignores_extras_and_missing():
    """裝了但不在鎖檔的套件（extras）與「鎖了未裝」（子集安裝）皆不報。"""
    assert g.venv_parity_drift("sometool==1.0\n", _LOCKMAP) == []
    assert g.venv_parity_drift("", _LOCKMAP) == []


def test_lock_version_map_skips_virtual_members():
    lock = tomllib.loads(_LOCK_OK)
    assert g.lock_version_map(lock) == {}  # 兩個條目皆 virtual → 不入版本表


def test_main_venv_advisory_never_flips_exit(monkeypatch):
    """advisory 有 drift 時 exit 仍由 Checks 1–3 決定（repo 乾淨 → 0）。"""
    monkeypatch.setattr(g, "venv_advisory", lambda *_a, **_k: ["[VENV] fake drift"])
    assert g.main(["--venv"]) == 0


def test_main_no_flag_behaviour_unchanged():
    assert g.main([]) == 0
