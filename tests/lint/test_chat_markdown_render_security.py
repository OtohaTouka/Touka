"""ToukaChat 的 Markdown 與流程圖只經固定的入口渲染（ADR-0294）.

模型輸出的 Markdown 圖片網址會被瀏覽器直接載入，可以把資料帶到外部網站。共用元件
``SafeMarkdown`` 固定用同一份規則決定圖片能不能載入；其他地方直接用 ``react-markdown``、
開啟原始 HTML，或把 HTML 直接塞進頁面，都會繞過這份規則。

流程圖（mermaid）畫圖時就會載入圖裡的外部圖片，所以 mermaid 只在沙箱框裡跑
（``mermaidSandbox.ts``）；頁面本身不載入 mermaid，也不載入框裡用的那份執行檔。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_ROOT = REPO_ROOT / "ToukaChat"
CHAT_SRC = CHAT_ROOT / "src"
SAFE_MARKDOWN = CHAT_SRC / "components" / "markdown" / "SafeMarkdown.tsx"
MERMAID_SANDBOX = CHAT_SRC / "lib" / "artifacts" / "mermaidSandbox.ts"
ARTIFACT_VIEW = CHAT_SRC / "components" / "artifacts" / "ArtifactView.tsx"
SOURCE_SUFFIXES = {".ts", ".tsx"}

# 唯一可以直接塞 HTML 的地方：流程圖，而且同一行要經過專屬的消毒函式
INNER_HTML_ALLOWED = {
    "ToukaChat/src/components/artifacts/ArtifactView.tsx": "sanitizeMermaidSvg(",
}


def _product_sources() -> list[Path]:
    return [
        path
        for path in CHAT_SRC.rglob("*")
        if path.suffix in SOURCE_SUFFIXES
        and "__tests__" not in path.parts
        and ".test." not in path.name
        and "generated" not in path.parts
    ]


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _module_pattern(module: str, *, value_only: bool = False) -> re.Pattern[str]:
    """``from``、``import()``、``require()`` 與只載入的 ``import "x"`` 都算；``value_only`` 時不算 ``import type``."""
    name = rf"""["']{re.escape(module)}(?:/[^"']*)?["']"""
    static = (
        rf"\bimport\s+(?!type\b)(?:(?!\bimport\b)[^;])*?\bfrom\s*{name}"
        if value_only
        else rf"\bfrom\s*{name}"
    )
    return re.compile(
        rf"{static}|\bexport\b[^;]*?\bfrom\s*{name}|\bimport\s*\(\s*{name}|\brequire\s*\(\s*{name}|\bimport\s+{name}"
    )


def _importers(pattern: re.Pattern[str]) -> list[str]:
    return [_rel(p) for p in _product_sources() if pattern.search(p.read_text(encoding="utf-8"))]


def test_only_safe_markdown_imports_react_markdown():
    assert _importers(_module_pattern("react-markdown")) == [_rel(SAFE_MARKDOWN)]


def test_module_pattern_covers_every_import_form():
    pattern = _module_pattern("mermaid", value_only=True)
    for text in (
        'import mermaid from "mermaid";',
        "import { render } from 'mermaid';",
        "import {\n  render,\n} from 'mermaid';",
        'export { default } from "mermaid";',
        'const m = await import("mermaid");',
        'import("mermaid/dist/mermaid.min.js")',
        'require("mermaid")',
        'import "mermaid";',
    ):
        assert pattern.search(text), text
    for text in (
        'import type { MermaidConfig } from "mermaid";',
        'import type { A } from "x"\nimport type { MermaidConfig } from "mermaid";',
        'import { x } from "mermaid-like";',
    ):
        assert not pattern.search(text), text


def test_mermaid_runs_only_in_the_sandbox_frame():
    assert _importers(_module_pattern("mermaid", value_only=True)) == []
    assert _importers(_module_pattern("@/generated/mermaidRuntime")) == [_rel(MERMAID_SANDBOX)]
    assert "renderMermaidInSandbox(" in ARTIFACT_VIEW.read_text(encoding="utf-8")


def test_safe_markdown_does_not_take_rehype_plugins():
    assert "rehypePlugins" not in SAFE_MARKDOWN.read_text(encoding="utf-8")


def test_raw_html_plugin_is_not_used():
    package = json.loads((CHAT_ROOT / "package.json").read_text(encoding="utf-8"))
    deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    assert "rehype-raw" not in deps
    assert [_rel(p) for p in _product_sources() if "rehype-raw" in p.read_text(encoding="utf-8")] == []


def test_inner_html_only_through_the_mermaid_sanitizer():
    found: list[str] = []
    for path in _product_sources():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "dangerouslySetInnerHTML" not in line:
                continue
            required = INNER_HTML_ALLOWED.get(_rel(path))
            if required is None or required not in line:
                found.append(f"{_rel(path)}: {line.strip()}")
    assert found == []
