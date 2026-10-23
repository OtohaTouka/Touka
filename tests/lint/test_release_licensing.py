"""Published images and the ToukaIDE VSIX must keep carrying their license notices.

- Every product image ships the project LICENSE and NOTICE in /app. touka-chat builds from
  ToukaChat/ only, so it takes them from the named build context `touka-root`, declared as an
  empty scratch stage: a build without that context fails instead of pulling an image by name.
- touka-chat's runtime install omits optional packages, which keeps sharp's LGPL libvips out.
- The image publish workflow only runs from main and pins every action to a commit SHA.
- ToukaIDE/THIRD_PARTY_NOTICES.txt lists the npm packages bundled into dist/extension.js.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHAT_RECIPE = ROOT / "ToukaChat" / "Dockerfile.prod"
HEARTBEAT_RECIPE = ROOT / "ToukaHeartbeat" / "Dockerfile"
HEARTBEAT_IGNORE = ROOT / "ToukaHeartbeat" / "Dockerfile.dockerignore"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "ghcr-publish.yml"
IDE_NOTICES = ROOT / "ToukaIDE" / "THIRD_PARTY_NOTICES.txt"
IDE_LOCK = ROOT / "ToukaIDE" / "package-lock.json"
LICENSE_FILES = ("LICENSE", "NOTICE")
NAMED_CONTEXT = "touka-root"


def _stage(recipe: str, name: str) -> str:
    """Text of one build stage, from its FROM line to the next FROM line."""
    start = re.search(rf"(?m)^FROM \S+ AS {re.escape(name)}$", recipe)
    assert start is not None, name
    rest = recipe[start.end() :]
    following = re.search(r"(?m)^FROM ", rest)
    return rest[: following.start()] if following else rest


def _ignored_by(rules_text: str, path: str) -> bool:
    """Docker ignore semantics for a top-level file name: the last matching rule wins."""
    ignored = False
    for raw in rules_text.splitlines():
        rule = raw.strip()
        if not rule or rule.startswith("#"):
            continue
        negated = rule.startswith("!")
        pattern = rule.removeprefix("!").removeprefix("/").removeprefix("**/")
        if fnmatchcase(path, pattern):
            ignored = not negated
    return ignored


def test_chat_runtime_omits_optional_packages() -> None:
    runner = _stage(CHAT_RECIPE.read_text(encoding="utf-8"), "runner")
    installs = re.findall(r"(?m)^RUN npm ci (.+)$", runner)

    assert len(installs) == 1
    assert "--omit=optional" in installs[0].split()


def test_chat_image_takes_license_files_from_the_named_context() -> None:
    recipe = CHAT_RECIPE.read_text(encoding="utf-8")

    assert re.search(rf"(?m)^FROM scratch AS {NAMED_CONTEXT}$", recipe)
    assert re.search(
        rf"(?m)^COPY --from={NAMED_CONTEXT} LICENSE NOTICE /touka-licenses/$",
        _stage(recipe, "builder"),
    )
    assert re.search(r"(?m)^COPY --from=builder /touka-licenses/ \./$", _stage(recipe, "runner"))


def test_heartbeat_image_copies_license_files_through_its_whitelist() -> None:
    recipe = HEARTBEAT_RECIPE.read_text(encoding="utf-8")
    rules = HEARTBEAT_IGNORE.read_text(encoding="utf-8")

    assert re.search(r"(?m)^COPY LICENSE NOTICE \./$", recipe)
    assert [name for name in LICENSE_FILES if _ignored_by(rules, name)] == []


def test_proxy_context_keeps_license_files() -> None:
    rules = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "COPY --chown=appuser:appuser . ." in (ROOT / "ToukaProxy" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert [name for name in LICENSE_FILES if _ignored_by(rules, name)] == []


def test_both_chat_builds_pass_the_named_context() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    build = compose["services"]["touka-chat"]["build"]
    assert build["dockerfile"] == "Dockerfile.prod"
    assert build["additional_contexts"] == {NAMED_CONTEXT: "."}
    assert re.search(rf"(?m)^\s+build-contexts: \|\n\s+{NAMED_CONTEXT}=\.$", workflow)


def test_publish_workflow_runs_from_main_with_pinned_actions() -> None:
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    jobs = yaml.safe_load(workflow)["jobs"]
    uses = re.findall(r"(?m)^\s+(?:- )?uses: (\S+)", workflow)

    assert {name: job.get("if") for name, job in jobs.items()} == {
        "publish": "github.ref == 'refs/heads/main'"
    }
    assert uses
    assert [ref for ref in uses if not re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref)] == []


def test_ide_notices_match_the_lockfile() -> None:
    """Every runtime package in the lock is listed, and no entry names a version the lock lacks.

    The list may carry a few more entries than the runtime set: esbuild bundles what the code
    imports, which can include packages the lock files under dev (has-flag, supports-color).
    Peer dependencies (hono) are not bundled into dist/extension.js and are not listed.
    """
    packages = json.loads(IDE_LOCK.read_text(encoding="utf-8"))["packages"]

    def identity(key: str, entry: dict) -> tuple[str, str]:
        return entry.get("name") or key.rsplit("node_modules/", 1)[-1], entry["version"]

    known = {identity(key, entry) for key, entry in packages.items() if key}
    runtime = {
        identity(key, entry)
        for key, entry in packages.items()
        if key and not any(entry.get(flag) for flag in ("dev", "optional", "devOptional", "peer"))
    }
    listed = set(
        re.findall(r"(?m)^=+\n(\S+) (\S+)\nLicense: ", IDE_NOTICES.read_text(encoding="utf-8"))
    )

    assert sorted(runtime - listed) == []
    assert sorted(listed - known) == []
