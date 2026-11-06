"""Published images and the ToukaIDE VSIX must keep carrying their license notices.

- Every product image ships the project LICENSE and NOTICE in /app. touka-chat builds from
  ToukaChat/ only, so it takes them from the named build context `touka-root`, declared as an
  empty scratch stage: a build without that context fails instead of pulling an image by name.
- touka-chat's runtime install omits optional packages, which keeps sharp's LGPL libvips out.
- The image publish workflow only runs from main and pins every action to a commit SHA.
- ToukaIDE/THIRD_PARTY_NOTICES.txt lists the npm packages bundled into dist/extension.js.
- An image that copies the node binary also copies Node's LICENSE from the same source, and
  one that copies uv also copies uv's two license texts, kept in ToukaProxy/licenses/uv because
  the official uv image carries none. NOTICE says where each of them lands. Only the stages that
  make up the image count (the last stage and the stages it is built FROM); build stages whose
  files are not copied over do not ship.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHAT_RECIPE = ROOT / "ToukaChat" / "Dockerfile.prod"
HEARTBEAT_RECIPE = ROOT / "ToukaHeartbeat" / "Dockerfile"
HEARTBEAT_IGNORE = ROOT / "ToukaHeartbeat" / "Dockerfile.dockerignore"
PROXY_RECIPE = ROOT / "ToukaProxy" / "Dockerfile"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "ghcr-publish.yml"
IDE_NOTICES = ROOT / "ToukaIDE" / "THIRD_PARTY_NOTICES.txt"
IDE_LOCK = ROOT / "ToukaIDE" / "package-lock.json"
LICENSE_FILES = ("LICENSE", "NOTICE")
NAMED_CONTEXT = "touka-root"
DOCKERFILE_GLOBS = ("Dockerfile*", "*/Dockerfile*", "tests/docker/Dockerfile*")

# The official node image keeps the binary in /usr/local/bin and Node's LICENSE, which also
# covers the libraries built into node, in /usr/local. Copying all of /usr/local brings both.
NODE_BINARY = re.compile(r"/usr/local/bin(?:/node)?/?")
NODE_LICENSE = re.compile(r"/usr/local(?:/LICENSE)?/?")
UV_BINARY = re.compile(r"/?uvx?")
UV_LICENSE_DIR = "ToukaProxy/licenses/uv"
# Git blob ids of the two files at astral-sh/uv tag 0.12.1, the uv the proxy image pins.
UV_LICENSES = {
    "LICENSE-MIT": "014835144877ea9c926d027ece3e1a26290cf481",
    "LICENSE-APACHE": "261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64",
}


@dataclass
class _Copy:
    source_image: str | None  # the --from value; None copies from the build context
    sources: list[str]
    destination: str


@dataclass
class _Stage:
    name: str | None
    base: str
    copies: list[_Copy] = field(default_factory=list)


def _instructions(recipe: str) -> list[tuple[str, str]]:
    """(KEYWORD, arguments) per instruction; continued lines are joined, comments dropped."""
    instructions: list[tuple[str, str]] = []
    pending = ""
    for raw in recipe.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        keyword, _, arguments = (pending + line).partition(" ")
        pending = ""
        # A heredoc body would be read as instructions; fail instead of checking the wrong thing.
        assert "<<" not in arguments, f"heredoc is not supported by this check: {keyword}"
        instructions.append((keyword.upper(), arguments.strip()))
    return instructions


def _copy(arguments: str) -> _Copy:
    """COPY or ADD: leading --flags (only --from matters), then sources and one destination."""
    source_image = None
    rest = arguments
    while rest.startswith("--"):
        flag, _, rest = rest.partition(" ")
        rest = rest.lstrip()
        if flag.startswith("--from="):
            source_image = flag.removeprefix("--from=").lower()
    paths = json.loads(rest) if rest.startswith("[") else rest.split()
    return _Copy(source_image, paths[:-1], paths[-1])


def _stages(recipe: str) -> list[_Stage]:
    stages: list[_Stage] = []
    for keyword, arguments in _instructions(recipe):
        if keyword == "FROM":
            words = [word for word in arguments.split() if not word.startswith("--")]
            name = words[2].lower() if len(words) == 3 and words[1].upper() == "AS" else None
            stages.append(_Stage(name, words[0].lower()))
        elif keyword in ("COPY", "ADD") and stages:
            stages[-1].copies.append(_copy(arguments))
    return stages


def _image_copies(recipe: str) -> list[_Copy]:
    """Copies made by the stages that end up in the image: the last one and its FROM chain."""
    stages = _stages(recipe)
    named = {stage.name: stage for stage in stages if stage.name}
    chain = [stages[-1]]
    while chain[-1].base in named and named[chain[-1].base] not in chain:
        chain.append(named[chain[-1].base])
    return [copy for stage in chain for copy in stage.copies]


def _node_license_copied(copies: list[_Copy], source_image: str) -> bool:
    return any(
        other.source_image == source_image and any(NODE_LICENSE.fullmatch(s) for s in other.sources)
        for other in copies
    )


def _license_violations(recipe: str) -> list[str]:
    copies = _image_copies(recipe)
    from_context = {s.rstrip("/") for c in copies if c.source_image is None for s in c.sources}
    problems: list[str] = []
    for c in copies:
        image = c.source_image
        copies_node = any(NODE_BINARY.fullmatch(s) for s in c.sources)
        if image is not None and copies_node and not _node_license_copied(copies, image):
            problems.append(f"node from {image} without /usr/local/LICENSE")
        if any(UV_BINARY.fullmatch(s) for s in c.sources) and UV_LICENSE_DIR not in from_context:
            missing = [
                name for name in UV_LICENSES if f"{UV_LICENSE_DIR}/{name}" not in from_context
            ]
            if missing:
                problems.append(f"uv from {c.source_image} without {', '.join(missing)}")
    return problems


def _license_destinations(recipe: str) -> list[str]:
    """Where the copied license files land in the image."""
    uv_texts = {f"{UV_LICENSE_DIR}/{name}" for name in UV_LICENSES}
    landed: list[str] = []
    for c in _image_copies(recipe):
        for source in c.sources:
            is_node = c.source_image is not None and source == "/usr/local/LICENSE"
            is_uv = c.source_image is None and source in uv_texts
            if not (is_node or is_uv):
                continue
            into_folder = c.destination.endswith("/") or len(c.sources) > 1
            name = PurePosixPath(source).name
            landed.append(f"{c.destination.rstrip('/')}/{name}" if into_folder else c.destination)
    return landed


def _dockerfiles() -> list[Path]:
    found = {path for pattern in DOCKERFILE_GLOBS for path in ROOT.glob(pattern)}
    return sorted(path for path in found if not path.name.endswith(".dockerignore"))


def _stage(recipe: str, name: str) -> str:
    """Text of one build stage, from its FROM line to the next FROM line."""
    start = re.search(rf"(?m)^FROM \S+ AS {re.escape(name)}$", recipe)
    assert start is not None, name
    rest = recipe[start.end() :]
    following = re.search(r"(?m)^FROM ", rest)
    return rest[: following.start()] if following else rest


def _ignored_by(rules_text: str, path: str) -> bool:
    """Docker ignore semantics: a rule applies when it matches the path or one of its parent
    folders, and the last matching rule wins. A `**/` rule matches at any depth."""
    parts = path.split("/")
    prefixes = ["/".join(parts[: index + 1]) for index in range(len(parts))]
    ignored = False
    for raw in rules_text.splitlines():
        rule = raw.strip()
        if not rule or rule.startswith("#"):
            continue
        negated = rule.startswith("!")
        pattern = rule.removeprefix("!").removeprefix("/").rstrip("/")
        names = set(prefixes)
        if pattern.startswith("**/"):
            pattern = pattern.removeprefix("**/")
            names = {"/".join(p.split("/")[i:]) for p in prefixes for i in range(p.count("/") + 1)}
        if any(fnmatchcase(name, pattern) for name in names):
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
    A runtime package esbuild leaves out of the bundle (hono) is still listed.
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


NODE_STAGE = "FROM node@sha256:aa AS node-runtime\nFROM python:3.12\n"
NODE_BINARY_COPY = "COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node\n"
NODE_LICENSE_COPY = "COPY --from=node-runtime /usr/local/LICENSE /usr/local/share/doc/\n"
UV_ARG = "ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.1\nFROM python:3.12\n"
UV_LICENSE_COPY = (
    "COPY ToukaProxy/licenses/uv/LICENSE-MIT ToukaProxy/licenses/uv/LICENSE-APACHE "
    "/usr/local/share/doc/uv/\n"
)
MISSING_LICENSES = {
    "node with flags": NODE_STAGE
    + "COPY --from=node-runtime --chown=1:1 --chmod=755 --link "
    + "/usr/local/bin/node /usr/local/bin/node\n",
    "node on a continued line": NODE_STAGE
    + "COPY --from=node-runtime \\\n    /usr/local/bin/node /usr/local/bin/node\n",
    "node license from another image": NODE_STAGE
    + NODE_BINARY_COPY
    + "COPY --from=other /usr/local/LICENSE /usr/local/share/doc/node/LICENSE\n",
    "node in the stage the image is built from": "FROM node@sha256:aa AS node-runtime\n"
    + "FROM python:3.12 AS base\nCOPY --from=node-runtime /usr/local/bin/ /usr/local/bin/\n"
    + "FROM base\nRUN true\n",
    "uv with several sources": "FROM python:3.12\n"
    + "COPY --link --from=ghcr.io/astral-sh/uv@sha256:bb /uv /uvx /bin/\n",
    "uv with one of its two licenses": "FROM python:3.12\nCOPY --from=uv-image /uv /bin/\n"
    + "COPY ToukaProxy/licenses/uv/LICENSE-MIT /usr/local/share/doc/uv/\n",
    "uv from an ARG image in json form": UV_ARG + 'COPY --from=${UV_IMAGE} ["/uv", "/bin/uv"]\n',
}
COMPLETE_LICENSES = {
    "node and its license": NODE_STAGE + NODE_BINARY_COPY + NODE_LICENSE_COPY,
    "uv from an ARG image with both licenses": UV_ARG
    + "COPY --chmod=755 --from=${UV_IMAGE} /uv /uvx /bin/\n"
    + UV_LICENSE_COPY,
    "uv licenses in json form": UV_ARG
    + "COPY --from=${UV_IMAGE} /uv /bin/\n"
    + 'COPY ["ToukaProxy/licenses/uv/LICENSE-MIT", "ToukaProxy/licenses/uv/LICENSE-APACHE", '
    + '"/usr/local/share/doc/uv/"]\n',
    "node only in a build stage": "FROM node@sha256:aa AS node-runtime\n"
    + "FROM python:3.12 AS builder\n"
    + NODE_BINARY_COPY
    + "FROM python:3.12\nCOPY --from=builder /build/out /srv/out\n",
    "the official node image as the base": "FROM node@sha256:aa AS node-runtime\n"
    + "FROM node-runtime AS runner\nCOPY --from=builder /build/out ./out\n",
}


@pytest.mark.parametrize("recipe", MISSING_LICENSES.values(), ids=MISSING_LICENSES.keys())
def test_license_check_catches_a_missing_license(recipe: str) -> None:
    assert _license_violations(recipe) != []


@pytest.mark.parametrize("recipe", COMPLETE_LICENSES.values(), ids=COMPLETE_LICENSES.keys())
def test_license_check_accepts_a_complete_image(recipe: str) -> None:
    assert _license_violations(recipe) == []


def test_every_product_dockerfile_is_scanned() -> None:
    scanned = {path.relative_to(ROOT).as_posix() for path in _dockerfiles()}

    assert {
        "ToukaProxy/Dockerfile",
        "ToukaHeartbeat/Dockerfile",
        "ToukaChat/Dockerfile",
        "ToukaChat/Dockerfile.prod",
    } <= scanned


@pytest.mark.parametrize(
    "recipe", _dockerfiles(), ids=lambda path: path.relative_to(ROOT).as_posix()
)
def test_images_that_copy_node_or_uv_carry_their_licenses(recipe: Path) -> None:
    assert _license_violations(recipe.read_text(encoding="utf-8")) == []


def test_proxy_image_keeps_node_and_uv_licenses_in_share_doc() -> None:
    assert sorted(_license_destinations(PROXY_RECIPE.read_text(encoding="utf-8"))) == [
        "/usr/local/share/doc/node/LICENSE",
        "/usr/local/share/doc/uv/LICENSE-APACHE",
        "/usr/local/share/doc/uv/LICENSE-MIT",
    ]


def test_notice_says_where_each_image_keeps_those_licenses() -> None:
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    landed = {
        destination
        for recipe in _dockerfiles()
        for destination in _license_destinations(recipe.read_text(encoding="utf-8"))
    }

    assert landed
    assert sorted(destination for destination in landed if destination not in notice) == []


def test_uv_license_texts_are_the_upstream_files() -> None:
    for name, blob in UV_LICENSES.items():
        data = (ROOT / UV_LICENSE_DIR / name).read_bytes()
        git_blob = hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False)
        assert git_blob.hexdigest() == blob, name


def test_proxy_context_keeps_uv_licenses() -> None:
    rules = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert [name for name in UV_LICENSES if _ignored_by(rules, f"{UV_LICENSE_DIR}/{name}")] == []


def test_ignore_check_follows_parent_folders() -> None:
    assert _ignored_by("docs/\n", "docs/licenses/LICENSE")
    assert _ignored_by("**/.toukadev\n", "a/.toukadev/LICENSE")
    assert not _ignored_by("docs/\n!docs/licenses/LICENSE\n", "docs/licenses/LICENSE")
    assert not _ignored_by("docs/\n", "ToukaProxy/licenses/LICENSE")
