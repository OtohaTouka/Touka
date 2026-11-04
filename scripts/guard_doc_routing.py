# ruff: noqa: T201, S603, S607
"""Doc Routing Guard — Guard #46（ADR-0296）。

規則分流後，`AGENTS.md` 只放全系統規則與一行索引，其餘全文在 `docs/rules/`。
這道守衛確認沒有規則因此掉出去：
- 禁令 1..最大號每條都有標籤，標到的模組都在 `docs/rules/modules.json`；
- 搬走的規則全文只有一份，在第一個模組的規則檔；沒搬走的不在 `docs/rules/`；
- `docs/rules/` 裡每份全文都有 `AGENTS.md` 的標籤指回來；
- 標了模組的規則有路徑，每個標到的模組都對得到檔案；
- 每個追蹤中的檔案都屬於某模組或明列不適用，每個 glob 至少對到一個檔案；
- 最後一條禁令落在 Codex 讀得到的範圍內（超過就失敗）。

規則文字本身的增刪由 PR 審查把關，這道守衛驗不到。
常駐文件超過大小上限只印 WARN，結束碼仍是 0：上限是提醒，不是規則。
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from applicable_rules import (
    AGENTS_FILE,
    MODULES_FILE,
    PROJECT_ROOT,
    Tag,
    all_rules,
    load_config,
    matches,
    parse_tags,
    read_lf,
)

if TYPE_CHECKING:
    from pathlib import Path

# Codex 只讀規則檔前 32 KiB（project_doc_max_bytes），禁令清單要全部讀得到。
CODEX_READ_LIMIT_BYTES = 32_768
AGENTS_SOFT_LIMIT_BYTES = 40_000
HANDOVER_SOFT_LIMIT_BYTES = 10_240
HANDOVER_FILE = "HANDOVER.md"

_BAN_START = re.compile(r"^\*\s+\*\*禁令 (?P<number>\d+)(?=\*\*|[^*]*\*\*)", re.MULTILINE)
_FULL_BAN = re.compile(r"^\*\s+\*\*禁令 (?P<number>\d+)(?=\*\*|\s*[（(])", re.MULTILINE)
_FULL_SECTION = re.compile(r"^### (?P<title>.+?)\s*$", re.MULTILINE)


@dataclass
class Report:
    """守衛結果：errors 讓結束碼非 0，warnings 只提醒。"""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def last_ban_end(text: str) -> int:
    """最後一條禁令那一行結束的位元組位置；沒有禁令時回 0。"""
    starts = list(_BAN_START.finditer(text))
    if not starts:
        return 0
    start = starts[-1].start()
    end = text.find("\n", start)
    return len(text[: end if end != -1 else len(text)].encode("utf-8"))


def _full_texts(root: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = defaultdict(list)
    for path in sorted((root / "docs" / "rules").glob("*.md")):
        name, text = path.relative_to(root).as_posix(), read_lf(path)
        found_here = [f"禁令 {m['number']}" for m in _FULL_BAN.finditer(text)]
        found_here += [m["title"] for m in _FULL_SECTION.finditer(text)]
        for key in found_here:
            found[key].append(name)
    return found


def _check_placement(
    key: str, tag: Tag, found: list[str], config: dict[str, Any], report: Report
) -> None:
    modules = config["modules"]
    unknown = [m for m in tag.modules if m not in modules]
    if unknown or not tag.modules:
        report.errors.append(f"{key} 標到不認得的模組：{'、'.join(unknown) or '（空）'}")
        return
    if tag.file is None:
        if key not in config.get("inline_rules", []):
            report.errors.append(f"{key} 標了模組卻沒有「全文：」，全文不能只剩摘要")
        if found:
            report.errors.append(f"{key} 全文在 AGENTS.md，docs/rules 不該再有一份：{found}")
        return
    first = modules[tag.modules[0]].get("rules_file")
    if tag.file != first:
        report.errors.append(f"{key} 的全文要放第一個模組的規則檔 {first}，標的是 {tag.file}")
    if not found:
        report.errors.append(f"{key} 在 {tag.file} 沒有全文")
    elif len(found) > 1:
        report.errors.append(f"{key} 的全文重複：{found}")
    elif found[0] != tag.file:
        report.errors.append(f"{key} 的全文在 {found[0]}，標的是 {tag.file}")


def _check_tags(root: Path, report: Report) -> None:
    config = load_config(root)
    tags = parse_tags(root)
    texts = _full_texts(root)
    for number in range(1, max(tags.bans, default=0) + 1):
        if number not in tags.bans:
            report.errors.append(f"禁令 {number} 不見了（編號要連續）")
    rules = all_rules(config, tags)
    for key, tag, _ in rules:
        found = texts.get(key, [])
        if isinstance(tag, Tag):
            _check_placement(key, tag, found, config, report)
        elif tag not in ("〔全系統〕", "〔退役〕"):
            report.errors.append(f"{key} 沒有標籤（〔全系統〕、〔退役〕或〔模組：…〕）")
        elif found:
            report.errors.append(f"{key} 全文在 AGENTS.md，docs/rules 不該再有一份：{found}")
    inline = {key for key, tag, _ in rules if isinstance(tag, Tag) and tag.file is None}
    for stale in sorted(set(config.get("inline_rules", [])) - inline):
        report.errors.append(f"inline_rules 的 {stale} 不是全文留在 AGENTS.md 的模組規則")
    for orphan in sorted(set(texts) - {key for key, _, _ in rules}):
        report.errors.append(f"{orphan} 在 {texts[orphan]} 有全文，AGENTS.md 沒有標籤指回來")
    tagged = {str(n) for n, tag in tags.bans.items() if isinstance(tag, Tag)}
    for extra in sorted(set(config["bans"]) - tagged, key=int):
        report.errors.append(f"禁令 {extra} 沒標模組，modules.json 的 bans 卻有 glob")
    for extra in sorted(set(config["sections"]) - set(tags.sections)):
        report.errors.append(f"{extra} 在 modules.json 的 sections，AGENTS.md 沒有標籤")


def _check_rule_paths(root: Path, tracked: list[str], report: Report) -> None:
    config = load_config(root)
    modules = config["modules"]
    for key, tag, globs in all_rules(config, parse_tags(root)):
        if not isinstance(tag, Tag):
            continue
        if not globs:
            report.errors.append(f"{key} 標了模組，{MODULES_FILE} 沒有它的路徑（空的也不行）")
            continue
        covered = [p for p in tracked if any(matches(g, p) for g in globs)]
        owned = {name: modules.get(name, {"globs": ["**"]})["globs"] for name in tag.modules}
        for name, spec in owned.items():
            if not any(matches(g, p) for g in spec for p in covered):
                report.errors.append(f"{key} 標了模組 {name}，路徑卻對不到這個模組的檔案")
        stray = [p for p in covered if not any(matches(g, p) for s in owned.values() for g in s)]
        if stray:
            report.errors.append(f"{key} 的路徑超出標到的模組：{stray[0]} 等 {len(stray)} 個檔案")


def _check_paths(root: Path, tracked: list[str], report: Report) -> None:
    config = load_config(root)
    modules = config["modules"]
    module_globs = [g for spec in modules.values() for g in spec["globs"]]
    recognized = [*module_globs, *config["not_applicable"]]
    for path in tracked:
        if not any(matches(g, path) for g in recognized):
            report.errors.append(f"{path} 不屬於任何模組，也沒有明列不適用（改 {MODULES_FILE}）")
    every_glob = {
        *recognized,
        *(g for globs in config["bans"].values() for g in globs),
        *(g for globs in config["sections"].values() for g in globs),
    }
    for glob in sorted(every_glob):
        if not any(matches(glob, path) for path in tracked):
            report.errors.append(f"glob {glob} 對不到任何追蹤中的檔案")
    for name, spec in modules.items():
        for key in ("rules_file", "handover"):
            target = spec.get(key)
            if target and not (root / target).is_file():
                report.errors.append(f"模組 {name} 的 {key} 不存在：{target}")


def _check_sizes(root: Path, report: Report) -> None:
    agents = read_lf(root / AGENTS_FILE)
    end = last_ban_end(agents)
    if end > CODEX_READ_LIMIT_BYTES:
        report.errors.append(
            f"最後一條禁令結束在第 {end} 位元組，超過 Codex 讀得到的 {CODEX_READ_LIMIT_BYTES}"
        )
    for name, limit in (
        (AGENTS_FILE, AGENTS_SOFT_LIMIT_BYTES),
        (HANDOVER_FILE, HANDOVER_SOFT_LIMIT_BYTES),
    ):
        size = len(read_lf(root / name).encode("utf-8"))
        if size > limit:
            report.warnings.append(f"{name} {size} 位元組，超過上限 {limit}")


def check(root: Path, tracked: list[str]) -> Report:
    """跑全部檢查。"""
    report = Report()
    _check_tags(root, report)
    _check_rule_paths(root, tracked, report)
    _check_paths(root, tracked, report)
    _check_sizes(root, report)
    return report


def _tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    )
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def main(root: Path = PROJECT_ROOT, tracked: list[str] | None = None) -> int:
    """命令列入口；有錯誤回 1。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    report = check(root, _tracked_files(root) if tracked is None else tracked)
    for warning in report.warnings:
        print(f"WARN: {warning}")
    for error in report.errors:
        print(f"ERROR: {error}")
    if report.errors:
        print(f"[FAIL] Doc Routing Guard：{len(report.errors)} 項")
        return 1
    print("[OK] Doc Routing Guard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
