"""Published images and the ToukaIDE VSIX must keep carrying their license notices.

- Every product image ships the project LICENSE and NOTICE in /app. touka-chat builds from
  ToukaChat/ only, so it takes them from the named build context `touka-root`, declared as an
  empty scratch stage: a build without that context fails instead of pulling an image by name.
- touka-chat's runtime install omits optional packages, which keeps sharp's LGPL libvips out.
- The image publish workflow only runs from main and pins every action to a commit SHA.
- ToukaIDE/THIRD_PARTY_NOTICES.txt lists the npm packages bundled into dist/extension.js.
- An image that holds node also holds Node's LICENSE, and one that holds uv also holds uv's two
  license texts, kept in ToukaProxy/licenses/uv because the official uv image carries none; NOTICE
  says where each of them lands. The check follows node, uv and the license files through the
  stages of every tracked Dockerfile: COPY and ADD with --from (a stage name, a stage number or an
  image, with global ARG defaults filled in), single files, whole folders and wildcards, and FROM
  another stage. The image is the last stage. The uv texts count only when a COPY names them or
  their folder; a bulk `COPY . .` does not. Not covered: files placed by RUN (for example
  `RUN --mount=from=... cp`). An image named by an ARG without a default is assumed to hold both
  node and uv, so a license taken from it is not checked against the image node came from. The
  parser fails on heredocs, an escape directive and a dangling line continuation instead of
  guessing.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
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

# Where the official images keep these files. Node's LICENSE also covers the libraries built
# into node; the uv image variants built on a Linux distribution keep uv in /usr/local/bin.
NODE_BINARY_PATH = "/usr/local/bin/node"
NODE_LICENSE_PATH = "/usr/local/LICENSE"
UV_PATHS = ("/uv", "/uvx", "/usr/local/bin/uv", "/usr/local/bin/uvx")
UV_LICENSE_DIR = "ToukaProxy/licenses/uv"
# The uv image the license texts were taken for (uv 0.12.1). When this digest changes, take
# LICENSE-MIT and LICENSE-APACHE from the new uv tag and update their git blob ids below.
UV_IMAGE = (
    "ghcr.io/astral-sh/uv@sha256:f6edac8c6d516a01a5387a8655c97481e0db46dd0a62ab62f41e5b2294376f6e"
)
UV_LICENSES = {
    "LICENSE-MIT": "014835144877ea9c926d027ece3e1a26290cf481",
    "LICENSE-APACHE": "261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64",
}
NODE, NODE_LICENSE, UV = "node", "Node.js LICENSE", "uv"
UV_LICENSE_KINDS = {name: f"uv {name}" for name in UV_LICENSES}

Contents = dict[str, set[str]]  # what a stage or image holds: kind -> paths


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
        if not instructions and not pending:
            # An escape directive changes the continuation character this parser relies on.
            assert not re.match(r"#\s*escape\s*=", line, re.IGNORECASE), "escape directive"
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        keyword, *rest = (pending + line).split(None, 1)
        pending = ""
        arguments = rest[0].strip() if rest else ""
        # A heredoc body would be read as instructions; fail instead of checking the wrong thing.
        assert "<<" not in arguments, f"heredoc is not supported by this check: {keyword}"
        instructions.append((keyword.upper(), arguments))
    assert not pending, "the last instruction ends with a line continuation"
    return instructions


def _copy(arguments: str) -> _Copy:
    """COPY or ADD: leading --flags (only --from matters), then sources and one destination."""
    source_image = None
    rest = arguments
    while rest.startswith("--"):
        flag, *tail = rest.split(None, 1)
        rest = tail[0] if tail else ""
        if flag.startswith("--from="):
            source_image = flag.removeprefix("--from=")
    paths = json.loads(rest) if rest.startswith("[") else rest.split()
    return _Copy(source_image, paths[:-1], paths[-1])


def _substitute(value: str, args: dict[str, str]) -> str:
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: args.get(m[1] or m[2], m[0]), value)


def _stages(recipe: str) -> list[_Stage]:
    global_args: dict[str, str] = {}
    stages: list[_Stage] = []
    for keyword, arguments in _instructions(recipe):
        if keyword == "ARG" and not stages:
            name, _, default = arguments.partition("=")
            if default:
                global_args[name.strip()] = default.strip().strip('"')
        elif keyword == "FROM":
            words = [word for word in arguments.split() if not word.startswith("--")]
            name = words[2].lower() if len(words) == 3 and words[1].upper() == "AS" else None
            stages.append(_Stage(name, _substitute(words[0], global_args)))
        elif keyword in ("COPY", "ADD") and stages:
            copy = _copy(arguments)
            if copy.source_image is not None:
                copy.source_image = _substitute(copy.source_image, global_args)
            stages[-1].copies.append(copy)
    return stages


def _repository(reference: str) -> str | None:
    """`node` for node@sha256:..., docker.io/library/node:24 and so on; None for an unfilled ARG."""
    if "$" in reference:
        return None
    return re.split(r"[@:]", reference.rsplit("/", 1)[-1])[0].lower()


def _image_contents(reference: str) -> Contents:
    """What an image pulled by reference holds, judged by its repository name."""
    repository = _repository(reference)
    contents: Contents = {}
    if repository in (None, "node"):
        contents[NODE] = {NODE_BINARY_PATH}
        contents[NODE_LICENSE] = {NODE_LICENSE_PATH}
    if repository in (None, "uv"):
        contents[UV] = set(UV_PATHS)
    return contents


def _matches(pattern: str, path: str) -> bool:
    """Docker path matching: * and ? stay within one path segment."""
    regex = re.escape(pattern).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(regex, path) is not None


def _ancestors(path: str, stop: str | None = None) -> list[str]:
    """The path and the folders above it, nearest first, up to `stop` when given."""
    chain = [path]
    current = PurePosixPath(path)
    while current != current.parent and str(current) != stop:
        current = current.parent
        chain.append(str(current))
    return chain


def _landing(copy: _Copy, source: str, path: str, stop: str | None = None) -> str | None:
    """Where `path` lands when `copy` names `source`: the file itself, a folder above it, or a
    wildcard matching either. None when this source does not reach the path."""
    pattern = source.rstrip("/") or "/"
    for folder in _ancestors(path, stop):
        if not _matches(pattern, folder):
            continue
        destination = PurePosixPath(copy.destination)
        if folder != path:
            return str(destination / PurePosixPath(path).relative_to(folder))
        into_folder = (
            copy.destination.endswith("/")
            or len(copy.sources) > 1
            or any(c in pattern for c in "*?")
        )
        return str(destination / PurePosixPath(path).name) if into_folder else copy.destination
    return None


def _copied(copy: _Copy, held: list[Contents], named: dict[str, int]) -> list[tuple[str, str]]:
    """(kind, landing path) for everything this copy brings in."""
    if copy.source_image is None:
        return [
            (kind, landing)
            for name, kind in UV_LICENSE_KINDS.items()
            for source in copy.sources
            if (
                landing := _landing(
                    copy,
                    source.removeprefix("./").lstrip("/"),
                    f"{UV_LICENSE_DIR}/{name}",
                    stop=UV_LICENSE_DIR,
                )
            )
        ]
    key = copy.source_image.lower()
    if key.isdigit() and int(key) < len(held):
        source_contents = held[int(key)]
    elif key in named:
        source_contents = held[named[key]]
    else:
        source_contents = _image_contents(copy.source_image)
    return [
        (kind, landing)
        for kind, paths in source_contents.items()
        for path in paths
        for source in copy.sources
        if (landing := _landing(copy, source, path))
    ]


def _final_contents(recipe: str) -> Contents:
    """What the image (the last stage) holds after every stage's FROM and copies."""
    held: list[Contents] = []
    named: dict[str, int] = {}
    for index, stage in enumerate(_stages(recipe)):
        base = named.get(stage.base.lower())
        start = held[base] if base is not None else _image_contents(stage.base)
        contents = {kind: set(paths) for kind, paths in start.items()}
        for copy in stage.copies:
            for kind, landing in _copied(copy, held, named):
                contents.setdefault(kind, set()).add(landing)
        held.append(contents)
        if stage.name:
            named[stage.name] = index
    return held[-1]


def _license_violations(recipe: str) -> list[str]:
    held = _final_contents(recipe)
    problems: list[str] = []
    if held.get(NODE) and not held.get(NODE_LICENSE):
        problems.append(f"node at {sorted(held[NODE])} without Node's LICENSE")
    if held.get(UV):
        missing = [name for name, kind in UV_LICENSE_KINDS.items() if not held.get(kind)]
        if missing:
            problems.append(f"uv at {sorted(held[UV])} without {', '.join(missing)}")
    return problems


def _license_destinations(recipe: str) -> list[str]:
    """Where the image keeps Node's LICENSE and the uv license texts."""
    held = _final_contents(recipe)
    kinds = [NODE_LICENSE, *UV_LICENSE_KINDS.values()]
    return sorted(path for kind in kinds for path in held.get(kind, set()))


def _dockerfiles() -> list[Path]:
    """Every tracked Dockerfile, wherever it sits in the repo; untracked local copies are not."""
    git = shutil.which("git")
    assert git is not None, "git is required for this check; it must not be skipped"
    listed = subprocess.run(  # noqa: S603 - resolved git, fixed argv, no shell
        [git, "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8")
    names = [
        name for name in listed.split("\0") if PurePosixPath(name).name.startswith("Dockerfile")
    ]
    return [ROOT / name for name in sorted(names) if not name.endswith(".dockerignore")]


def _stage(recipe: str, name: str) -> str:
    """Text of one build stage, from its FROM line to the next FROM line."""
    start = re.search(rf"(?m)^FROM \S+ AS {re.escape(name)}$", recipe)
    assert start is not None, name
    rest = recipe[start.end() :]
    following = re.search(r"(?m)^FROM ", rest)
    return rest[: following.start()] if following else rest


def _ignored_by(rules_text: str, path: str) -> bool:
    """Docker ignore semantics: a rule applies when it matches the path or one of its parent
    folders, and the last matching rule wins. A `**/` rule matches at any depth.

    This is close to Docker, not equal: fnmatch lets `*` cross `/` and a `**` in the middle of a
    rule does not match zero folders. A COPY of a file Docker does exclude still fails the build.
    """
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


NODE_DONOR = "FROM node@sha256:aa AS node-runtime\n"
NODE_STAGE = NODE_DONOR + "FROM python:3.12\n"
NODE_BINARY_COPY = "COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node\n"
NODE_LICENSE_COPY = "COPY --from=node-runtime /usr/local/LICENSE /usr/local/share/doc/\n"
NODE_BUILDER = NODE_DONOR + "FROM python:3.12 AS builder\n" + NODE_BINARY_COPY
UV_ARG = "ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.1\nFROM python:3.12\n"
UV_DEPS = "FROM python:3.12 AS deps\nCOPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /bin/\n"
UV_ONLY = "FROM python:3.12\nCOPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /bin/\n"
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
    "node after a tab": NODE_STAGE
    + "COPY\t--from=node-runtime\t/usr/local/bin/node /usr/local/bin/\n",
    "node by stage number": NODE_STAGE + "COPY --from=0 /usr/local/bin/node /usr/local/bin/node\n",
    "node by wildcard": NODE_STAGE + "COPY --from=node-runtime /usr/local/bin/* /usr/local/bin/\n",
    "node license from another image": NODE_STAGE
    + NODE_BINARY_COPY
    + "COPY --from=other /usr/local/LICENSE /usr/local/share/doc/node/LICENSE\n",
    "node in the stage the image is built from": NODE_DONOR
    + "FROM python:3.12 AS base\nCOPY --from=node-runtime /usr/local/bin/ /usr/local/bin/\n"
    + "FROM base\nRUN true\n",
    "node through a builder's /usr/local": NODE_BUILDER
    + "FROM python:3.12\nCOPY --from=builder /usr/local /usr/local\n",
    "node license from a builder that has none": NODE_BUILDER
    + "FROM python:3.12\nCOPY --from=builder /usr/local/bin/node /usr/local/bin/node\n"
    + "COPY --from=builder /usr/local/ /opt/\n",
    "node in an image put together from scratch": NODE_BUILDER
    + "FROM scratch\nCOPY --from=builder / /\n",
    "uv with several sources": "FROM python:3.12\n"
    + "COPY --link --from=ghcr.io/astral-sh/uv@sha256:bb /uv /uvx /bin/\n",
    "uv with one of its two licenses": UV_ONLY
    + "COPY ToukaProxy/licenses/uv/LICENSE-MIT /usr/local/share/doc/uv/\n",
    "uv from an ARG image in json form": UV_ARG + 'COPY --from=${UV_IMAGE} ["/uv", "/bin/uv"]\n',
    "uv from an ARG without a default": "FROM python:3.12\nCOPY --from=${UV_SOURCE} /uv /bin/\n",
    "uv through a deps stage": UV_DEPS
    + "FROM python:3.12\nCOPY --from=deps /bin/uv /bin/uvx /bin/\n",
    "uv from an image variant": "FROM python:3.12\n"
    + "COPY --from=ghcr.io/astral-sh/uv:python3.12-bookworm /usr/local/bin/uv /bin/\n",
    "uv with the repository copied in bulk": UV_ONLY + "COPY . .\n",
    "uv from a stage named after it": "FROM ghcr.io/astral-sh/uv:0.12.1 AS uv-image\n"
    + "FROM python:3.12\nCOPY --from=uv-image /uv /bin/\n",
}
COMPLETE_LICENSES = {
    "node and its license": NODE_STAGE + NODE_BINARY_COPY + NODE_LICENSE_COPY,
    "all of /usr/local from the node image": NODE_STAGE
    + "COPY --from=node-runtime /usr/local /usr/local\n",
    "/usr/local/bin from a builder without node": "FROM python:3.12 AS builder\n"
    + "FROM python:3.12\nCOPY --from=builder /usr/local/bin /usr/local/bin\n",
    "uv from an ARG image with both licenses": UV_ARG
    + "COPY --chmod=755 --from=${UV_IMAGE} /uv /uvx /bin/\n"
    + UV_LICENSE_COPY,
    "uv licenses in json form": UV_ARG
    + "COPY --from=${UV_IMAGE} /uv /bin/\n"
    + 'COPY ["ToukaProxy/licenses/uv/LICENSE-MIT", "ToukaProxy/licenses/uv/LICENSE-APACHE", '
    + '"/usr/local/share/doc/uv/"]\n',
    "uv licenses as a folder": UV_DEPS
    + "FROM python:3.12\nCOPY --from=deps /bin/uv /bin/\n"
    + "COPY ./ToukaProxy/licenses/uv/ /usr/local/share/doc/uv\n",
    "node only in a build stage": NODE_BUILDER
    + "FROM python:3.12\nCOPY --from=builder /build/out /srv/out\n",
    "the official node image as the base": NODE_DONOR
    + "FROM node-runtime AS runner\nCOPY --from=builder /build/out ./out\n",
}
UNREADABLE_RECIPES = {
    "heredoc": "FROM python:3.12\nRUN <<EOF\necho hi\nEOF\n",
    "escape directive": "# escape=`\nFROM python:3.12\n",
    "dangling continuation": "FROM python:3.12\nRUN echo hi \\\n",
}


@pytest.mark.parametrize("recipe", MISSING_LICENSES.values(), ids=MISSING_LICENSES.keys())
def test_license_check_catches_a_missing_license(recipe: str) -> None:
    assert _license_violations(recipe) != []


@pytest.mark.parametrize("recipe", COMPLETE_LICENSES.values(), ids=COMPLETE_LICENSES.keys())
def test_license_check_accepts_a_complete_image(recipe: str) -> None:
    assert _license_violations(recipe) == []


@pytest.mark.parametrize("recipe", UNREADABLE_RECIPES.values(), ids=UNREADABLE_RECIPES.keys())
def test_license_check_refuses_what_it_cannot_read(recipe: str) -> None:
    with pytest.raises(AssertionError):
        _license_violations(recipe)


def test_license_destinations_follow_folder_copies() -> None:
    recipe = COMPLETE_LICENSES["uv licenses as a folder"]

    assert _license_destinations(recipe) == [
        "/usr/local/share/doc/uv/LICENSE-APACHE",
        "/usr/local/share/doc/uv/LICENSE-MIT",
    ]


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
    assert _license_destinations(PROXY_RECIPE.read_text(encoding="utf-8")) == [
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


def test_dockerfiles_use_the_uv_image_the_license_texts_belong_to() -> None:
    references = set()
    for recipe in _dockerfiles():
        stages = _stages(recipe.read_text(encoding="utf-8"))
        names = {stage.name for stage in stages if stage.name}
        references |= {stage.base for stage in stages}
        references |= {
            copy.source_image
            for stage in stages
            for copy in stage.copies
            if copy.source_image and copy.source_image.lower() not in names
        }

    assert {ref for ref in references if _repository(ref) == "uv"} == {UV_IMAGE}


def test_proxy_context_keeps_uv_licenses() -> None:
    rules = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert [name for name in UV_LICENSES if _ignored_by(rules, f"{UV_LICENSE_DIR}/{name}")] == []


def test_ignore_check_follows_parent_folders() -> None:
    assert _ignored_by("docs/\n", "docs/licenses/LICENSE")
    assert _ignored_by("**/.toukadev\n", "a/.toukadev/LICENSE")
    assert not _ignored_by("docs/\n!docs/licenses/LICENSE\n", "docs/licenses/LICENSE")
    assert not _ignored_by("docs/\n", "ToukaProxy/licenses/LICENSE")
