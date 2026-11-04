# ruff: noqa: T201, S603, S607
"""列出這次改動適用的規則與交班檔（ADR-0296）。

只用標準函式庫、不載入專案程式（禁令 63）。資料只有兩處：`AGENTS.md` 的標籤
（規則→模組、全文放哪）與 `docs/rules/modules.json`（模組與規則→路徑）。

    python scripts/applicable_rules.py                    # 和 origin/dev 的分叉點比
    python scripts/applicable_rules.py --paths a.py b.py  # 計畫階段還沒有改動時
    python scripts/applicable_rules.py --print-rules      # 連適用規則的全文一起印

認不出的路徑一律要求讀全部規則檔；算不出比對基準就失敗，不當成沒有改動。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
AGENTS_FILE = "AGENTS.md"
MODULES_FILE = "docs/rules/modules.json"
DEFAULT_BASE_REF = "origin/dev"
EXIT_NO_BASE = 2

_BAN_LINE = re.compile(r"^\*\s+\*\*禁令 (?P<number>\d+)[^*]*\*\*(?P<tag>〔[^〕]*〕)?")
_SECTION_HEADING = re.compile(r"^### (?P<title>.+?)\s*$")
_ANY_HEADING = re.compile(r"^#{1,3} ")
_MODULE_TAG = re.compile(r"〔模組：(?P<modules>[^〕]*)〕")
_FULL_TEXT = re.compile(r"全文：(?P<file>docs/rules/[^\s。]+\.md)\s*$")


class RoutingError(Exception):
    """算不出這次改了什麼。"""


@dataclass(frozen=True)
class Tag:
    """一條規則在 `AGENTS.md` 的標籤。`file` 是 None 表示全文就在 `AGENTS.md`。"""

    modules: tuple[str, ...]
    file: str | None


@dataclass
class Tags:
    """`AGENTS.md` 裡每條禁令與小節的標籤。"""

    bans: dict[int, Tag | str | None] = field(default_factory=dict)
    sections: dict[str, Tag] = field(default_factory=dict)


@dataclass
class Route:
    """一組路徑適用的模組、規則與交班檔。"""

    paths: list[str]
    modules: list[str]
    items: list[tuple[str, str]]
    handovers: list[str]
    unknown: list[str]
    every_rules_file: list[str]
    every_handover: list[str]


def all_rules(config: dict[str, Any], tags: Tags) -> list[tuple[str, Tag | str | None, list[str]]]:
    """每條規則的名稱、標籤與路徑。腳本與 Guard #46 共用。"""
    return [
        *((f"禁令 {n}", t, config["bans"].get(str(n), [])) for n, t in sorted(tags.bans.items())),
        *((title, t, config["sections"].get(title, [])) for title, t in tags.sections.items()),
    ]


@cache
def _compile(pattern: str) -> re.Pattern[str]:
    parts = pattern.split("/")
    out = ""
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            out += ".*" if last else "(?:[^/]+/)*"
            continue
        for char in part:
            out += "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char)
        out += "" if last else "/"
    return re.compile(out)


def matches(pattern: str, path: str) -> bool:
    """`/` 分隔、分大小寫，`**` 可跨資料夾。腳本與 Guard #46 共用這一個比對函式。"""
    return _compile(pattern).fullmatch(path) is not None


def read_lf(path: Path) -> str:
    """Windows 工作資料夾是 CRLF；量大小與比對都以追蹤中的 LF 為準。"""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def load_config(root: Path) -> dict[str, Any]:
    """讀 `docs/rules/modules.json`。"""
    return json.loads(read_lf(root / MODULES_FILE))


def _tag_of(text: str) -> Tag | None:
    found = _MODULE_TAG.search(text)
    if found is None:
        return None
    full_text = _FULL_TEXT.search(text)
    modules = tuple(m.strip() for m in found.group("modules").split("、") if m.strip())
    return Tag(modules, full_text.group("file") if full_text else None)


def parse_tags(root: Path) -> Tags:
    """從 `AGENTS.md` 讀出每條規則的標籤。"""
    tags = Tags()
    section: str | None = None
    for line in read_lf(root / AGENTS_FILE).splitlines():
        ban = _BAN_LINE.match(line)
        if ban:
            raw = ban.group("tag")
            module_tag = _tag_of(line) if raw and raw.startswith("〔模組：") else None
            tags.bans[int(ban.group("number"))] = module_tag or raw
            continue
        heading = _SECTION_HEADING.match(line)
        if heading:
            section = heading.group("title")
            continue
        if section and line.startswith("〔模組：") and (tag := _tag_of(line)):
            tags.sections[section] = tag
    return tags


def route(paths: Any, config: dict[str, Any], tags: Tags) -> Route:
    """算出這組路徑適用什麼；認不出的路徑放進 `unknown`。"""
    normalized = sorted({p.replace("\\", "/").removeprefix("./") for p in paths})
    modules = config["modules"]
    touched: list[str] = []
    unknown: list[str] = []
    for path in normalized:
        owners = [m for m, spec in modules.items() if any(matches(g, path) for g in spec["globs"])]
        touched.extend(owners)
        if not owners and not any(matches(g, path) for g in config["not_applicable"]):
            unknown.append(path)

    items = [
        (key, tag.file or AGENTS_FILE)
        for key, tag, globs in all_rules(config, tags)
        if isinstance(tag, Tag) and any(matches(g, p) for g in globs for p in normalized)
    ]

    ordered = [m for m in modules if m in touched]
    every_rules = sorted({s["rules_file"] for s in modules.values() if s.get("rules_file")})
    every_handover = sorted({s["handover"] for s in modules.values()})
    handovers = list(dict.fromkeys(modules[m]["handover"] for m in ordered))
    return Route(normalized, ordered, items, handovers, unknown, every_rules, every_handover)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        message = f"git {args[0]} 失敗：{detail}"
        raise RoutingError(message)
    return result.stdout.decode("utf-8")


def changed_paths(root: Path, base: str | None = None) -> list[str]:
    """和分叉點比：已提交、已暫存、未暫存都算；改名列舊新兩個路徑；未追蹤也算。"""
    try:
        start = _git(root, "merge-base", base or DEFAULT_BASE_REF, "HEAD").strip()
    except RoutingError as exc:
        hint = "用 --base 指定，或用 --paths 列路徑"
        message = f"算不出比對基準（{base or DEFAULT_BASE_REF}）：{exc}；{hint}"
        raise RoutingError(message) from exc
    found: set[str] = set()
    for args in (
        ("diff", "--name-only", "--no-renames", "-z", start),
        ("diff", "--name-only", "--no-renames", "-z", "--cached", start),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        found.update(p for p in _git(root, *args).split("\0") if p)
    return sorted(found)


def expand_dirs(root: Path, paths: list[str]) -> list[str]:
    """資料夾展開成底下的追蹤檔；空資料夾與新路徑照原樣，之後由 `route` 判斷。"""
    found: list[str] = []
    for path in paths:
        listed = _git(root, "ls-files", "-z", "--", path) if (root / path).is_dir() else ""
        found.extend([p for p in listed.split("\0") if p] or [path])
    return found


def render(result: Route) -> str:
    """把結果排成給人看的文字。"""
    lines = [f"改動路徑 {len(result.paths)} 個；模組：{'、'.join(result.modules) or '無'}"]
    lines.append("全系統規則在 AGENTS.md，一律適用。")
    if not result.paths:
        lines.append("沒有改動；還沒動手時用 --paths 列出預計要改的路徑。")
    if result.unknown:
        lines.append("認不出的路徑：")
        lines.extend(f"  {p}" for p in result.unknown)
        lines.append("所以要讀全部規則檔與交班檔：")
        lines.extend(f"  {p}" for p in [*result.every_rules_file, *result.every_handover])
    if result.items:
        lines.append("適用的規則（讀全文，或加 --print-rules）：")
        lines.extend(f"  {key}：{where}" for key, where in result.items)
    if result.handovers:
        lines.append("交班檔：")
        lines.extend(f"  {p}" for p in result.handovers)
    return "\n".join(lines)


def _ban_text(text: str, number: int) -> str | None:
    found = (m for m in map(_BAN_LINE.match, text.splitlines()) if m)
    return next((m.string for m in found if int(m["number"]) == number), None)


def _section_text(text: str, title: str) -> str | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == f"### {title}":
            later = range(index + 1, len(lines))
            end = next((i for i in later if _ANY_HEADING.match(lines[i])), len(lines))
            return "\n".join(lines[index:end]).rstrip()
    return None


def full_text(result: Route, root: Path) -> str:
    """取出適用規則的全文。"""
    chunks = []
    for key, where in result.items:
        text = read_lf(root / where)
        number = key.removeprefix("禁令 ")
        chunk = _ban_text(text, int(number)) if number.isdigit() else _section_text(text, key)
        chunks.append(chunk or f"{key}：{where} 裡找不到全文")
    return "\n\n".join(chunks)


def main(argv: list[str] | None = None, root: Path = PROJECT_ROOT) -> int:
    """命令列入口；算不出比對基準回 2。"""
    parser = argparse.ArgumentParser(description="列出這次改動適用的規則與交班檔")
    parser.add_argument("--paths", nargs="+", help="直接指定路徑（計畫階段用）")
    parser.add_argument("--base", help=f"比對基準，預設 {DEFAULT_BASE_REF} 的分叉點")
    parser.add_argument("--print-rules", action="store_true", help="連適用規則的全文一起印")
    args = parser.parse_args(argv)
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        paths = expand_dirs(root, args.paths) if args.paths else changed_paths(root, args.base)
    except RoutingError as exc:
        print(exc, file=sys.stderr)
        return EXIT_NO_BASE
    result = route(paths, load_config(root), parse_tags(root))
    print(render(result))
    if args.print_rules and result.items:
        print("\n" + full_text(result, root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
