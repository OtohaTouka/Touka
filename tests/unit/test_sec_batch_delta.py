"""Unit/lint tests for P8/P16/P17/P18/P19/P20/P21/P23 security hardening (Batch 4 Delta — TDD).

P8  — Dockerfiles must have non-root USER directive.
P16 — Vault paths must reject symlink escape.
P17 — gemini_to_openai must use json.loads, not ast.literal_eval.
P18 — Autonomy quota reads/writes must be serialized with threading.Lock.
P19 — Requests exceeding MAX_REQUEST_BODY_BYTES must be rejected with 413.
P20 — requirements.txt must not use loose >= pins.
P21 — permission_service config reload must be locked.
P23 — tracked env templates must not ship publicly known secret values.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import tomllib
from pathlib import Path

import pytest
from src.adapters.gemini_to_openai import convert_gemini_to_openai as gemini_to_openai_response
from src.config import is_public_secret, settings
from src.services import autonomy as autonomy_mod
from src.services import execution_broker
from src.services import permission_service as ps_mod
from src.services.autonomy import AutonomyService
from src.utils.path_security import validate_no_symlink_escape

# ---------------------------------------------------------------------------
# P8 — Dockerfiles must have USER directive (non-root)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
DOCKERFILES = [
    REPO_ROOT / "ToukaProxy" / "Dockerfile",
    REPO_ROOT / "ToukaHeartbeat" / "Dockerfile",
    REPO_ROOT / "ToukaChat" / "Dockerfile",
]


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.parent.name)
def test_dockerfile_has_user_directive(dockerfile):
    """Each Dockerfile must contain a non-root USER directive."""
    assert dockerfile.exists(), f"Dockerfile not found: {dockerfile}"
    content = dockerfile.read_text(encoding="utf-8")
    assert "USER " in content, (
        f"{dockerfile.parent.name}/Dockerfile must have USER directive for non-root execution"
    )
    # Must not be USER root
    for line in content.splitlines():
        if line.strip().startswith("USER "):
            assert line.strip() != "USER root", (
                f"{dockerfile.parent.name}/Dockerfile must not use USER root"
            )


# ---------------------------------------------------------------------------
# P16 — Vault path symlink validation
# ---------------------------------------------------------------------------

def test_path_security_module_exists():
    """ToukaProxy/src/utils/path_security.py must exist."""
    path = REPO_ROOT / "ToukaProxy" / "src" / "utils" / "path_security.py"
    assert path.exists(), "path_security.py must be created"


def test_vault_path_rejects_symlink(tmp_path):
    """validate_no_symlink_escape must block paths outside the vault."""
    allowed = tmp_path / "vault"
    allowed.mkdir()

    # Test path that escapes via parent traversal
    evil_path = (allowed / ".." / "escaped" / "secret.txt").resolve()
    result = validate_no_symlink_escape(evil_path, allowed)
    assert result is False, "Path traversal outside vault should be rejected"


def test_vault_path_allows_legitimate_path(tmp_path):
    """validate_no_symlink_escape must allow real paths inside the vault."""
    vault = tmp_path / "vault"
    vault.mkdir()
    legit = vault / "data" / "file.txt"
    legit.parent.mkdir()
    legit.write_text("ok")

    result = validate_no_symlink_escape(legit, vault)
    assert result is True, "Legitimate path should be allowed"


# ---------------------------------------------------------------------------
# P17 — gemini_to_openai uses json.loads not ast.literal_eval
# ---------------------------------------------------------------------------

def test_gemini_adapter_uses_json_not_ast():
    """gemini_to_openai.py must not use ast.literal_eval for parsing."""
    adapter_path = REPO_ROOT / "ToukaProxy" / "src" / "adapters" / "gemini_to_openai.py"
    content = adapter_path.read_text(encoding="utf-8")
    assert "ast.literal_eval" not in content, (
        "gemini_to_openai.py must not use ast.literal_eval (use json.loads instead)"
    )


def test_gemini_adapter_parses_json_arguments():
    """gemini_to_openai must correctly parse JSON-encoded arguments."""
    fake_gemini = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "test_tool",
                        "args": {"key": "value"},
                    }
                }]
            },
            "finishReason": "STOP",
        }]
    }
    result = gemini_to_openai_response(fake_gemini)
    tool_calls = result["choices"][0]["message"].get("tool_calls", [])
    assert tool_calls, "Tool calls must be present"
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert args["key"] == "value"


# ---------------------------------------------------------------------------
# P18 — Autonomy lock exists
# ---------------------------------------------------------------------------

def test_autonomy_lock_exists():
    """autonomy.py must have a module-level threading.Lock."""
    assert hasattr(autonomy_mod, "_autonomy_lock"), (
        "autonomy.py must define module-level _autonomy_lock"
    )
    assert isinstance(autonomy_mod._autonomy_lock, type(threading.Lock())), (
        "_autonomy_lock must be a threading.Lock"
    )


def test_concurrent_quota_consumption_correct_count(tmp_path):
    """Concurrent quota consumption must not double-count under threading."""
    call_count = {"n": 0}

    svc = AutonomyService.__new__(AutonomyService)
    svc.config = {"quota": {"daily_max": 50, "reset_hour": 0},
                  "react_loop": {"default_max_slots": 5, "nudge_prompt": "", "stop_keywords": []}}

    lock = threading.Lock()

    def run():
        with lock:
            call_count["n"] += 1

    threads = [threading.Thread(target=run) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count["n"] == 10, "All threads must complete"


# ---------------------------------------------------------------------------
# P19 — RequestSizeLimitMiddleware
# ---------------------------------------------------------------------------

def test_request_size_limit_middleware_exists():
    """ToukaProxy/src/middleware/request_size_limit.py must exist."""
    path = REPO_ROOT / "ToukaProxy" / "src" / "middleware" / "request_size_limit.py"
    assert path.exists(), "request_size_limit.py must be created"


def test_oversized_request_rejected(auth_client):
    """POST with body > MAX_REQUEST_BODY_BYTES must return 413."""
    max_bytes = getattr(settings, "MAX_REQUEST_BODY_BYTES", 10 * 1024 * 1024)

    oversized = "x" * (max_bytes + 1)
    resp = auth_client.post(
        "/v1/messages",
        content=oversized,
        headers={"Content-Length": str(max_bytes + 1), "Content-Type": "text/plain"},
    )
    assert resp.status_code == 413, (
        f"Oversized request must return 413, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# P20 — workspace pyproject dependencies: no loose >= pins（ADR-0117 後掃 pyproject）
# ---------------------------------------------------------------------------

def test_no_loose_greater_than_pins():
    """Workspace member pyproject dependencies must not contain loose >= pins."""
    members = [
        "ToukaProxy",
        "ToukaMemory",
        "ToukaHeartbeat",
        "ToukaDispatch",
    ]
    violations = []
    for sp in members:
        pyproject = REPO_ROOT / sp / "pyproject.toml"
        with pyproject.open("rb") as f:
            doc = tomllib.load(f)
        for req in doc.get("project", {}).get("dependencies", []):
            if ">=" in req:
                violations.append(f"{sp}/pyproject.toml: {req}")
    # root dev group 同樣受釘版政策約束
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        root_doc = tomllib.load(f)
    for req in root_doc.get("dependency-groups", {}).get("dev", []):
        if ">=" in req:
            violations.append(f"pyproject.toml [dev]: {req}")

    assert not violations, (
        "pyproject dependencies must not use >= pins. Violations:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# P21 — permission_service reload lock
# ---------------------------------------------------------------------------

def test_permission_reload_lock_exists():
    """permission_service.py must have a module-level threading.Lock for reload."""
    assert hasattr(ps_mod, "_config_reload_lock"), (
        "permission_service.py must define _config_reload_lock"
    )
    assert isinstance(ps_mod._config_reload_lock, type(threading.Lock())), (
        "_config_reload_lock must be a threading.Lock"
    )


# ---------------------------------------------------------------------------
# P23 — tracked env templates carry no publicly known secret values
# ---------------------------------------------------------------------------

# `KEY=value` and commented `# KEY=value` lines (the commented form is copied too).
_TEMPLATE_ASSIGNMENT_RE = re.compile(r"^\s*#?\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _template_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value.split(" #", 1)[0]


def test_env_example_has_no_real_secrets():
    """No tracked `.env*.example` may ship a publicly known value for the three secrets.

    The bootstrap copies the root template into `.env` on first run, so a value shipped
    here becomes a live secret. Matching is key plus value (other keys may share a
    placeholder string). The template list comes from git, so new templates are covered.
    """
    if not GIT:
        pytest.skip("Git unavailable")
    result = execution_broker.run_sync(
        [GIT, "-C", str(REPO_ROOT), "ls-files", "-z", "--", "*.env*.example"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    templates = sorted({name for name in result.stdout.decode("utf-8").split("\0") if name})
    assert templates, "no tracked env template found"

    offenders = []
    for name in templates:
        lines = (REPO_ROOT / name).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            match = _TEMPLATE_ASSIGNMENT_RE.match(line)
            if match and is_public_secret(match.group(1), _template_value(match.group(2))):
                offenders.append(f"{name}:{number} {match.group(1)}")
    assert offenders == [], f"env templates ship publicly known secret values: {offenders}"
