"""Guard #20 的 knip 不靠 .gitignore 判斷檔案，也不因此變鬆。

`guard:prune` 以 `--no-gitignore` 執行：根 `.gitignore` 有 `.toukadev/`，knip 會用它
濾掉絕對路徑含 `.toukadev` 的入口檔，`.toukadev/worktrees/` 底下的工作資料夾因此假紅
（ADR-0243 已知限制）。關掉 gitignore 後，原本靠 gitignore 擋掉的產生檔與建置產物，
必須在 `knip.config.ts` 的 `project` 以排除樣式移出相依圖：只放 `ignore` 的話，這些檔
仍會被分析，它們的引用會讓沒用到的 export 被當成有用到。

本測試只讀文字檔，不執行 node、不載入專案程式。下表是被 gitignore 的路徑逐條處理的
清單：值是 knip `project` 必須有的排除樣式；`None` 表示不需要，理由寫在旁邊。
任一 .gitignore 新增或刪除規則（會落在 ToukaChat 底下的），本測試會要求同步分類。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHAT_DIR = PROJECT_ROOT / "ToukaChat"
KNIP_CONFIG = CHAT_DIR / "knip.config.ts"

# knip 5 的預設 project 樣式；覆寫 project 時必須保留，否則沒有任何檔被分析、守衛形同關閉。
DEFAULT_PROJECT_GLOB = "**/*.{js,mjs,cjs,jsx,ts,tsx,mts,cts}!"

# ToukaChat/.gitignore（路徑相對於 ToukaChat）
CHAT_RULES: dict[str, str | None] = {
    "/storybook-static/": "!storybook-static/**",  # 建置產物
    "/node_modules": None,  # knip 全域排除 **/node_modules/**
    "/.pnp": None,  # Yarn PnP；本專案只用 npm，不會產生
    ".pnp.*": None,  # 同上
    ".yarn/*": None,  # knip 全域排除 .yarn；本專案只用 npm
    "/coverage": "!coverage/**",  # 測試覆蓋率報告（含 JS）
    "/.next/": "!.next/**",  # Next 建置產物
    "/out/": "!out/**",  # Next 靜態輸出
    "/build": "!build/**",  # 建置產物
    ".DS_Store": None,  # 不是程式
    "*.pem": None,  # 不是程式
    ".vscode/": None,  # 編輯器設定，不是程式
    "npm-debug.log*": None,  # 日誌
    "yarn-debug.log*": None,  # 日誌
    "yarn-error.log*": None,  # 日誌
    ".pnpm-debug.log*": None,  # 日誌
    ".env*": None,  # 環境變數檔，副檔名不在 project 樣式內
    ".vercel": "!**/.vercel/**",  # 部署建置產物
    "*.tsbuildinfo": None,  # tsc 增量狀態，副檔名不在 project 樣式內
    "next-env.d.ts": "!**/next-env.d.ts",  # Next 產生的型別宣告
    "src/generated/": "!src/generated/**",  # 產生檔（artifact runtime bundle，ADR-0114）
}

# 根 .gitignore 中會落在 ToukaChat 底下的規則（沒有開頭 `/`、中間也沒有 `/` 的才會在任何深度生效）
ROOT_RULES: dict[str, str | None] = {
    ".env*": None,  # 環境變數檔
    "heartbeat-config.json": None,  # 設定檔，不是程式
    "heartbeat-config/": None,  # 設定目錄，不是程式
    "venv/": None,  # Python 虛擬環境
    ".venv/": None,  # Python 虛擬環境
    "env/": None,  # Python 虛擬環境
    "*.egg-info/": None,  # Python 打包資訊
    "__pycache__/": None,  # Python 快取
    "*.py[cod]": None,  # Python 位元組碼
    "*$py.class": None,  # Jython 位元組碼
    ".pytest_cache/": None,  # pytest 快取
    ".mypy_cache/": None,  # mypy 快取
    ".ruff_cache": None,  # ruff 快取
    "node_modules/": None,  # knip 全域排除 **/node_modules/**
    ".vite/": "!**/.vite/**",  # Vite／Vitest 建置快取（含 JS）
    "data/": None,  # 實體資料目錄名，不是程式
    "backups/": None,  # 備份目錄名，不是程式
    "*.db": None,  # 資料庫檔
    "*.log": None,  # 日誌
    ".vscode/": None,  # 編輯器設定
    ".gemini/": None,  # 工具設定
    ".antigravitycli": None,  # 工具設定
    ".claude/": None,  # 工具設定
    ".codex/": None,  # 工具設定
    ".toukadev/": None,  # 開發流程本機紀錄；正是造成工作資料夾假紅的規則
    ".DS_Store": None,  # 不是程式
    "Thumbs.db": None,  # 不是程式
    ".VSCodeCounter/": None,  # 工具輸出，不是程式
    ".import_linter_cache": None,  # 工具快取
    "HANDOVER_HEARTBEAT.md": None,  # 文件
    "SNS.md": None,  # 文件
    "repomix-output.xml": None,  # 工具輸出
    "溝通協調.md": None,  # 文件
    "touka-docs-repomix.xml": None,  # 工具輸出
    ".repomixignore": None,  # 工具設定
    "touka-code-repomix.xml": None,  # 工具輸出
}


def _gitignore_rules(path: Path) -> list[str]:
    rules = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rule = line.strip()
        if rule and not rule.startswith("#"):
            rules.append(rule)
    return rules


def _applies_below_root(rule: str) -> bool:
    """根 .gitignore 的規則會不會在子目錄（ToukaChat）裡生效。"""
    body = rule.removeprefix("!")
    return not body.startswith("/") and "/" not in body.rstrip("/")


def _knip_string_array(key: str) -> list[str]:
    text = KNIP_CONFIG.read_text(encoding="utf-8")
    match = re.search(rf"^\s*{key}:\s*\[(.*?)^\s*\],", text, re.MULTILINE | re.DOTALL)
    assert match, f"knip.config.ts 找不到 {key} 陣列"
    values = []
    for line in match.group(1).splitlines():
        code = line.split("//", 1)[0]
        values.extend(re.findall(r'"([^"]+)"', code))
    return values


def test_guard_prune_does_not_filter_entries_through_gitignore():
    scripts = json.loads((CHAT_DIR / "package.json").read_bytes())["scripts"]
    assert "--no-gitignore" in scripts["guard:prune"].split()


def test_knip_project_keeps_default_source_glob():
    assert DEFAULT_PROJECT_GLOB in _knip_string_array("project")


def test_gitignored_generated_and_build_paths_are_outside_knip_project():
    project = set(_knip_string_array("project"))
    expected = {glob for glob in (*CHAT_RULES.values(), *ROOT_RULES.values()) if glob}
    missing = sorted(expected - project)
    assert missing == [], f"knip project 缺少排除樣式：{missing}"


def test_every_gitignore_rule_under_chat_is_classified():
    chat_rules = {
        rule for rule in _gitignore_rules(CHAT_DIR / ".gitignore") if not rule.startswith("!")
    }
    root_rules = {
        rule
        for rule in _gitignore_rules(PROJECT_ROOT / ".gitignore")
        if not rule.startswith("!") and _applies_below_root(rule)
    }
    assert chat_rules == set(CHAT_RULES), (
        f"ToukaChat/.gitignore 與分類表不一致：新增 {sorted(chat_rules - set(CHAT_RULES))}，"
        f"已刪 {sorted(set(CHAT_RULES) - chat_rules)}"
    )
    assert root_rules == set(ROOT_RULES), (
        f"根 .gitignore 與分類表不一致：新增 {sorted(root_rules - set(ROOT_RULES))}，"
        f"已刪 {sorted(set(ROOT_RULES) - root_rules)}"
    )
