"""Real installed build tools with synthetic input and the existing test sandbox."""

from __future__ import annotations

import base64
import importlib.metadata
from pathlib import Path

import markdown
import pip_api
import pytest
from mkdocs.config import load_config
from pip._internal.models.link import Link

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("absolute", [False, True], ids=["relative-traversal", "absolute-path"])
def test_b64_default_cannot_embed_an_image_outside_its_base(tmp_path: Path, absolute: bool) -> None:
    base = tmp_path / "docs"
    base.mkdir()
    outside = tmp_path / "outside.png"
    content = b"synthetic-private-image-boundary-marker"
    outside.write_bytes(content)
    source = outside.as_posix() if absolute else "../outside.png"
    converter = markdown.Markdown(
        extensions=["pymdownx.b64"], extension_configs={"pymdownx.b64": {"base_path": str(base)}}
    )

    rendered = converter.convert(f'<img src="{source}">')

    assert base64.b64encode(content).decode("ascii") not in rendered
    assert "data:image/png;base64," not in rendered


def test_pip_link_does_not_decode_path_separator_twice() -> None:
    link = Link("https://example.invalid/%252foutside.whl")
    assert link.filename == "%2foutside.whl"
    assert "/" not in link.filename


def test_pip_link_preserves_normal_filename_decoding() -> None:
    assert Link("https://example.invalid/example%20package.whl").filename == "example package.whl"


def test_project_markdown_extensions_keep_rendering_supported_content() -> None:
    configuration = load_config(config_file=str(REPO_ROOT / "mkdocs.yml"))
    converter = markdown.Markdown(
        extensions=configuration.markdown_extensions,
        extension_configs=configuration.mdx_configs,
    )
    rendered = converter.convert(
        '??? note "Details"\n\n    Nested text.\n\n'
        "```mermaid\ngraph TD\n    A --> B\n```\n\n"
        '=== "Tab"\n\n    Tab content.\n\n:smile:\n'
    )

    assert "<details" in rendered
    assert 'class="mermaid"' in rendered
    assert 'class="tabbed-set' in rendered
    assert "twemoji" in rendered
    assert "<img " in rendered


def test_pip_api_parses_local_requirements_with_the_installed_pip(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")

    parsed = pip_api.parse_requirements(str(requirements))

    assert str(parsed["example-pkg"].specifier) == "==1.0.0"
    assert str(pip_api.PIP_VERSION) == importlib.metadata.version("pip")
