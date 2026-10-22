"""Tracked files must work on any machine and must not let local secrets slip into git.

- The bootstrap writes the first admin password to `ToukaProxy/config/INITIAL_ADMIN_PASSWORD.txt`
  (`src/config.py`), and the scaffold writes agent-built MCP servers under `config/mcp_servers/`;
  the `config/*.json` rule covers neither, so `git add -A` would commit them.
- Each compose sets `HOST_WORKSPACE_ROOT` for the proxy. Its default must be where the workspace
  is mounted inside the container, not a path that only exists on one host.
- A tracked `.env*.example` is copied into a real `.env`, so an active line pointing at a drive
  letter breaks every other machine.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from src.services import execution_broker

if TYPE_CHECKING:
    import subprocess

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")

MUST_BE_IGNORED = (
    "ToukaProxy/config/INITIAL_ADMIN_PASSWORD.txt",
    "ToukaProxy/config/mcp_servers/example_server/server.py",
    # Skills and evals an agent creates at runtime; only the ones the repo ships stay tracked.
    "ToukaProxy/config/skills/agent-made-skill/SKILL.md",
    "ToukaProxy/config/evals/mcp/agent_made_server.eval.md",
    "ToukaProxy/config/evals/skill/agent_made_skill.eval.md",
)
COMPOSES = ("docker-compose.yml", "docker-compose.ghcr.yml", "ToukaProxy/docker-compose.yml")
_WORKSPACE_DEFAULT = re.compile(r"\$\{WORKSPACE_ROOT:-([^}]*)\}")
_ACTIVE_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_DRIVE_PATH = re.compile(r"""^['"]?[A-Za-z]:[\\/]""")


def _git(*args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    if not GIT:
        pytest.skip("Git unavailable")
    return execution_broker.run_sync(
        [GIT, "-C", str(REPO_ROOT), *args],
        input=stdin,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_local_secret_files_are_ignored() -> None:
    """Only a non-negated rule from a `.gitignore` in the repo counts.

    A `!` rule still prints a match; a rule from a developer's global excludes file or
    `.git/info/exclude` would not travel with a clone.
    """
    stdin = "\0".join(MUST_BE_IGNORED).encode("utf-8") + b"\0"
    result = _git("check-ignore", "--no-index", "--stdin", "-z", "--verbose", stdin=stdin)
    assert result.returncode in (0, 1), result.stderr
    fields = result.stdout.decode("utf-8").split("\0")[:-1]
    # --verbose -z: source, line number, pattern, path for every matched path.
    records = [fields[index : index + 4] for index in range(0, len(fields), 4)]
    ignored = {
        path
        for source, _, rule, path in records
        if rule
        and not rule.startswith("!")
        and Path(source).name == ".gitignore"
        and not Path(source).is_absolute()
    }

    assert [path for path in MUST_BE_IGNORED if path not in ignored] == []


def _proxy_environment(service: dict) -> dict[str, str]:
    raw = service.get("environment", {})
    if isinstance(raw, dict):
        return {key: str(value) for key, value in raw.items()}
    return dict(item.split("=", 1) for item in raw if "=" in item)


def _mounts(service: dict) -> dict[str, str]:
    """Mount target to source."""
    found = {}
    for item in service.get("volumes", []):
        if isinstance(item, dict):
            found[item["target"]] = str(item.get("source", ""))
        else:
            cut = item.find(":/")
            found[item[cut + 1 :].split(":")[0]] = item[:cut]
    return found


def _proxy(rel: str) -> dict:
    return yaml.safe_load((REPO_ROOT / rel).read_text(encoding="utf-8"))["services"]["touka-proxy"]


def _workspace_root(proxy: dict) -> tuple[str, str]:
    """(raw value, default it falls back to when WORKSPACE_ROOT is unset)."""
    value = _proxy_environment(proxy)["HOST_WORKSPACE_ROOT"]
    default = _WORKSPACE_DEFAULT.fullmatch(value)
    return value, default[1] if default else value


def _workspace_root_default(proxy: dict) -> str:
    return _workspace_root(proxy)[1]


@pytest.mark.parametrize("rel", COMPOSES)
def test_proxy_workspace_root_defaults_to_the_container_mount(rel: str) -> None:
    """The default must be one of the proxy's own mount targets, the same in every compose.

    The value follows WORKSPACE_ROOT only when that mount's source does too; otherwise
    setting WORKSPACE_ROOT would point the value at a folder that is not mounted there.
    """
    proxy = _proxy(rel)
    value, default = _workspace_root(proxy)
    mounts = _mounts(proxy)

    assert default in mounts
    assert ("WORKSPACE_ROOT" in value) == ("WORKSPACE_ROOT" in mounts[default])
    assert default == _workspace_root_default(_proxy(COMPOSES[0]))


def test_env_templates_have_no_active_drive_letter_path() -> None:
    result = _git("ls-files", "-z", "--", "*.env*.example")
    assert result.returncode == 0, result.stderr
    templates = sorted({name for name in result.stdout.decode("utf-8").split("\0") if name})
    assert templates, "no tracked env template found"

    offenders = []
    for name in templates:
        lines = (REPO_ROOT / name).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            match = _ACTIVE_ASSIGNMENT.match(line)
            if match and _DRIVE_PATH.match(match.group(2).strip()):
                offenders.append(f"{name}:{number} {match.group(1)}")
    assert offenders == []
