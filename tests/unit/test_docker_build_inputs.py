"""Check product Dockerfile recipes and their build-context ignore rules (static only)."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
RECIPES = (
    ("ToukaProxy/Dockerfile", "touka-proxy"),
    ("ToukaHeartbeat/Dockerfile", "touka-heartbeat"),
    ("ToukaChat/Dockerfile.prod", "touka-chat"),
)
DEBIAN_RECIPES = (
    *(path for path, _product in RECIPES),
    "ToukaChat/Dockerfile",
)
# (recipe, context root relative to repo, ignore file relative to repo)
CONTEXTS = {
    "ToukaProxy/Dockerfile": ("", ".dockerignore"),
    "ToukaHeartbeat/Dockerfile": ("", "ToukaHeartbeat/Dockerfile.dockerignore"),
    "ToukaChat/Dockerfile.prod": ("ToukaChat", "ToukaChat/.dockerignore"),
    "ToukaChat/Dockerfile": ("ToukaChat", "ToukaChat/.dockerignore"),
}
IMMUTABLE_IMAGE = r"[^\s@]+@sha256:[0-9a-f]{64}"


def _external_images(recipe: str) -> list[str]:
    stages = set(re.findall(r"(?im)^FROM\s+\S+\s+AS\s+(\S+)", recipe))
    # Docker's empty base is not a registry input; COPY operands still require checking.
    sources = [
        source for source in re.findall(r"(?im)^FROM\s+(\S+)", recipe) if source != "scratch"
    ]
    sources += re.findall(r"(?im)^COPY\s+--from=(\S+)", recipe)
    return [source for source in sources if source not in stages]


@pytest.mark.parametrize(("path", "product"), RECIPES)
def test_all_external_build_inputs_are_immutable(path, product):
    recipe = (ROOT / path).read_text(encoding="utf-8")
    images = _external_images(recipe)
    assert images, product
    assert all(re.fullmatch(IMMUTABLE_IMAGE, image) for image in images), images


@pytest.mark.parametrize(
    ("statement", "external"),
    [
        ("", None),
        ("FROM registry.invalid/mutable:latest", "registry.invalid/mutable:latest"),
        (
            "COPY --from=registry.invalid/mutable:latest /synthetic /synthetic",
            "registry.invalid/mutable:latest",
        ),
        ("FROM scratch:tag", "scratch:tag"),
        ("COPY --from=scratch /synthetic /synthetic", "scratch"),
    ],
    ids=("empty-base", "mutable-from", "mutable-copy", "tagged-scratch", "scratch-copy"),
)
def test_empty_base_does_not_hide_unpinned_external_inputs(
    statement: str, external: str | None
) -> None:
    pinned = "registry.invalid/base@sha256:" + "a" * 64
    recipe = (
        "FROM scratch AS empty\n"
        f"FROM {pinned} AS runtime\n"
        "COPY --from=empty /synthetic /synthetic\n"
        f"{statement}\n"
    )
    images = _external_images(recipe)
    assert images == [pinned] + ([] if external is None else [external])
    immutable = all(re.fullmatch(IMMUTABLE_IMAGE, image) for image in images)
    assert immutable is (external is None)


@pytest.mark.parametrize("path", DEBIAN_RECIPES)
def test_active_debian_recipes_replace_wolfi_and_apk(path):
    recipe = (ROOT / path).read_text(encoding="utf-8")
    assert re.search(r"(?m)^FROM python:3\.12-slim-trixie@sha256:[0-9a-f]{64}(?:\s|$)", recipe)
    assert "cgr.dev/chainguard/wolfi-base@" not in recipe
    assert not re.search(r"\bapk\s+(?:add|upgrade|del)\b", recipe)


@pytest.mark.parametrize(("path", "product"), RECIPES)
def test_runtime_product_labels_bind_source(path, product):
    recipe = (ROOT / path).read_text(encoding="utf-8")
    assert f'org.opencontainers.image.title="{product}"' in recipe
    assert 'org.opencontainers.image.revision="${TOUKA_SOURCE_REVISION}"' in recipe
    assert 'org.opencontainers.image.source="https://github.com/OtohaTouka/Touka"' in recipe


@pytest.mark.parametrize(
    "private_name", ("HANDOVER.md", ".codex", ".agents", ".github", ".cache", "**/.toukadev")
)
def test_root_ignore_declares_known_private_context_exclusions(private_name):
    rules = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    normalized = [rule.strip().rstrip("/") for rule in rules if not rule.lstrip().startswith("#")]
    assert private_name in normalized
    assert "!" + private_name not in normalized


def test_heartbeat_reuses_proxy_python_and_uv_identity():
    proxy = _external_images((ROOT / RECIPES[0][0]).read_text(encoding="utf-8"))
    heartbeat_recipe = (ROOT / RECIPES[1][0]).read_text(encoding="utf-8")
    heartbeat = _external_images(heartbeat_recipe)
    python_images = {
        image
        for image in proxy
        if re.fullmatch(r"python:3\.12-slim-trixie@sha256:[0-9a-f]{64}", image)
    }
    assert len(python_images) == 1, "Proxy 必須只有一個共用的 Debian Python identity"
    (python,) = python_images
    uv = next(image for image in proxy if image.startswith("ghcr.io/astral-sh/uv@"))
    stages = re.findall(r"(?im)^FROM\s+(\S+)(?:\s+AS\s+([^\r\n]+))?$", heartbeat_recipe)
    assert stages == [(python, "deps"), (python, "")]
    assert heartbeat.count(uv) == 1


def test_product_copy_and_lifecycle_boundaries_remain_intact():
    proxy = (ROOT / RECIPES[0][0]).read_text(encoding="utf-8")
    heartbeat = (ROOT / RECIPES[1][0]).read_text(encoding="utf-8")
    chat = (ROOT / RECIPES[2][0]).read_text(encoding="utf-8")
    assert "COPY --chown=appuser:appuser . ." in proxy
    assert "COPY --chown=appuser:appuser ToukaHeartbeat/ ." in heartbeat
    assert "uv sync --locked --no-dev" in proxy and "uv sync --locked --no-dev" in heartbeat
    assert all("--ignore-scripts" in command for command in re.findall(r"(?m)^RUN npm (.+)$", chat))
    assert all("USER appuser" in recipe for recipe in (proxy, heartbeat, chat))


def test_chat_runtime_does_not_inherit_python_builder():
    recipe = (ROOT / "ToukaChat/Dockerfile.prod").read_text(encoding="utf-8")
    donor = re.search(r"(?m)^FROM node@sha256:[0-9a-f]{64} AS ([\w-]+)$", recipe)
    runner = re.search(r"(?m)^FROM (\S+) AS runner$", recipe)
    assert donor is not None and runner is not None
    assert runner[1] == donor[1], "Chat runner 必須直接使用 Node runtime，不能繼承 Python builder"
    runtime = recipe[runner.end() :]
    assert not re.search(r"(?m)^ENTRYPOINT\b", runtime)
    assert not re.search(r"(?m)^COPY --from=\S+ /usr/local/ /usr/local/", runtime)
    assert not re.search(r"(?m)^COPY --from=(?!builder )", runtime)
    assert not re.search(r"\bapt-get\s+install\b[^;&]*\b(?:python\S*|make|g\+\+)(?:\s|$)", runtime)


def _synthetic_root_entry_included(relative: Path, rules: list[str]) -> bool:
    """Project only root-entry rules; actual Docker precedence is BuildKit's."""
    included = True
    for raw in rules:
        rule = raw.strip().rstrip("/")
        if not rule or rule.startswith("#"):
            continue
        negate = rule.startswith("!")
        pattern = rule[1:] if negate else rule
        if fnmatchcase(relative.parts[0], pattern):
            included = negate
    return included


def _finite_context_included(relative: Path, rules: list[str]) -> bool:
    """Test only current literal/*/** rules; BuildKit remains authoritative."""
    parts = relative.as_posix().split("/")
    assert all(part not in {"", ".", ".."} for part in parts)

    def matches(names: list[str], patterns: list[str]) -> bool:
        if not patterns:
            return not names
        if patterns[0] == "**":
            return matches(names, patterns[1:]) or bool(names) and matches(names[1:], patterns)
        return (
            bool(names) and fnmatchcase(names[0], patterns[0]) and matches(names[1:], patterns[1:])
        )

    included = True
    for raw in rules:
        rule = raw.strip()
        if not rule or rule.startswith("#"):
            continue
        negate = rule.startswith("!")
        pattern = rule.removeprefix("!").rstrip("/")
        segments = pattern.split("/")
        if not all(
            segment not in {"", ".", ".."}
            and re.fullmatch(r"[\w.*-]+", segment)
            and ("**" not in segment or segment == "**")
            for segment in segments
        ):
            raise ValueError("UNSUPPORTED_CONTEXT_RULE")
        if any(matches(parts[:length], segments) for length in range(1, len(parts) + 1)):
            included = negate
    return included


def _product_context_rules(product: str) -> list[str]:
    relative = {
        "touka-proxy": ".dockerignore",
        "touka-heartbeat": "ToukaHeartbeat/Dockerfile.dockerignore",
        "touka-chat": "ToukaChat/.dockerignore",
    }[product]
    return (ROOT / relative).read_text(encoding="utf-8").splitlines()


def _explicit_copy_sources(recipe: str) -> list[str]:
    """Literal context sources of COPY (no --from, no globs, not the whole context)."""
    sources = []
    for line in re.findall(r"(?m)^COPY\s+(?!--from=)(.+)$", recipe):
        operands = [token for token in line.split() if not token.startswith("--")]
        sources += [
            source.rstrip("/") for source in operands[:-1] if source != "." and "*" not in source
        ]
    return sources


@pytest.mark.parametrize("recipe_path", sorted(CONTEXTS))
def test_explicit_copy_sources_exist_and_survive_context_rules(recipe_path):
    context, ignore = CONTEXTS[recipe_path]
    sources = _explicit_copy_sources((ROOT / recipe_path).read_text(encoding="utf-8"))
    assert sources, recipe_path
    rules = (ROOT / ignore).read_text(encoding="utf-8").splitlines()
    for source in sources:
        assert (ROOT / context / source).exists(), source
        assert _finite_context_included(Path(source), rules), source


@pytest.mark.parametrize(
    "name",
    [
        "backport.mjs",
        "manifest.json",
        "braces-3.0.3-depth-limit.patch",
    ],
)
def test_chat_recipes_copy_braces_backport_inputs(name):
    source = f"container-security/braces-backport/{name}"
    for recipe_path in ("ToukaChat/Dockerfile.prod", "ToukaChat/Dockerfile"):
        assert source in _explicit_copy_sources((ROOT / recipe_path).read_text(encoding="utf-8"))


@pytest.mark.parametrize("product", ["touka-proxy", "touka-heartbeat"])
@pytest.mark.parametrize("name", ["src/private.ts", "node_modules/private.js", "out/private.html"])
def test_python_contexts_do_not_include_chat_application(product, name):
    assert not _finite_context_included(Path("ToukaChat") / name, _product_context_rules(product))


@pytest.mark.parametrize(
    "name",
    [
        "node_modules/private.js",
        ".venv/private.py",
        "__pycache__/private.pyc",
        ".env",
        "nested/.env.local",
        "nested/data/private.json",
        "node_modules/.env.example",
        "nested/data/.env.example",
    ],
)
def test_proxy_context_never_ships_nested_chat_secrets_or_caches(name):
    assert not _finite_context_included(
        Path("ToukaChat/container-security") / name, _product_context_rules("touka-proxy")
    )


@pytest.mark.parametrize("product", [product for _, product in RECIPES])
@pytest.mark.parametrize("name", ["data/marker.txt", ".env", "HANDOVER.md", ".codex/marker.txt"])
def test_product_contexts_keep_private_exclusions(product, name):
    assert not _finite_context_included(Path(name), _product_context_rules(product))


@pytest.mark.parametrize(
    "name",
    [
        ".toukadev/worktrees/x/AGENTS.md",
        ".toukadev/lessons/synthetic.md",
        "ToukaProxy/src/.toukadev/x.md",
    ],
)
def test_proxy_root_context_excludes_toukadev_workspace(name):
    """Root-anchored rules such as AGENTS.md miss a worktree copy; prune `.toukadev` anywhere."""
    assert not _finite_context_included(Path(name), _product_context_rules("touka-proxy"))


@pytest.mark.parametrize("ignore", sorted({ignore for _, ignore in CONTEXTS.values()}))
def test_context_exceptions_have_no_wildcards(ignore):
    """A wildcard `!` rule makes BuildKit walk every excluded folder, `.toukadev` included."""
    rules = (ROOT / ignore).read_text(encoding="utf-8").splitlines()
    exceptions = [rule.strip() for rule in rules if rule.strip().startswith("!")]
    assert not [rule for rule in exceptions if any(char in rule for char in "*?[")]


def test_proxy_context_ships_config_templates_but_not_live_config():
    rules = _product_context_rules("touka-proxy")
    templates = sorted((ROOT / "ToukaProxy" / "config").glob("*.json.example"))
    assert templates
    for template in templates:
        assert _finite_context_included(template.relative_to(ROOT), rules), template.name
    assert not _finite_context_included(Path("ToukaProxy/config/models.json"), rules)


@pytest.mark.parametrize("rule", ["!", "src/?", "src/[ab]", "src\\name", "../src", "src/**x"])
def test_finite_context_projection_rejects_unsupported_rules(rule):
    with pytest.raises(ValueError, match="UNSUPPORTED_CONTEXT_RULE"):
        _finite_context_included(Path("src/visible.py"), [rule])


@pytest.mark.parametrize(
    "exclusion", ["container-security", "container-security/**", "*/braces-backport"]
)
def test_context_projection_detects_later_exclusion_of_required_input(exclusion):
    rules = _product_context_rules("touka-chat")
    relative = Path("container-security/braces-backport/manifest.json")
    assert _finite_context_included(relative, rules)
    assert not _finite_context_included(relative, [*rules, exclusion])


@pytest.mark.parametrize("control", ("HANDOVER.md", ".codex", ".agents", ".github"))
def test_chat_ignore_excludes_synthetic_controls_and_keeps_application_input(tmp_path, control):
    relative = Path(control) if control.endswith(".md") else Path(control) / "synthetic.txt"
    marker = tmp_path / relative
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("synthetic private control marker")
    visible = tmp_path / "src" / "visible.ts"
    visible.parent.mkdir()
    visible.write_text("synthetic application input")
    rules = (ROOT / "ToukaChat" / ".dockerignore").read_text(encoding="utf-8").splitlines()
    projected = {
        path.relative_to(tmp_path).as_posix(): path.read_text()
        for path in (marker, visible)
        if _synthetic_root_entry_included(path.relative_to(tmp_path), rules)
    }
    assert projected == {"src/visible.ts": "synthetic application input"}
