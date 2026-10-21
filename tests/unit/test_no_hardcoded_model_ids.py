"""選模型的介面與預設值不寫死廠商模型 ID（ADR-0250 A2）。

寫死的模型名會過時：廠商下架或改名後，畫面預設、新對話換回的模型、IDE 行內補全都會
指向不存在的模型。這些地方一律留空，等模型清單載入後挑第一個可用的一般模型，
或交由使用者設定。

本測試只讀文字檔，不執行 node、不載入專案程式。範圍是下列選模型與主要模型的檔案；
價格表、context window 表等需要列出模型的資料表不在範圍。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

GUARDED_FILES = (
    "ToukaChat/src/components/layout/SoulStudio.tsx",
    "ToukaChat/src/components/layout/ModelStudio.tsx",
    "ToukaChat/src/lib/providers/ChatProvider.tsx",
    "ToukaChat/src/lib/providers/ModelProvider.tsx",
    "ToukaChat/src/lib/default_model.ts",
    "ToukaIDE/src/providers/InlineCompletionProvider.ts",
    "ToukaIDE/package.json",
    "ToukaProxy/src/utils/brain_persistence.py",
    "ToukaProxy/src/services/custom_model_service.py",
)

# 「廠商前綴-版本或型號」形狀的模型 ID，例如 claude-4.6-sonnet、gemini-2.0-flash-exp。
MODEL_ID = re.compile(r"\b(?:claude|gemini|gpt|deepseek|grok)-[0-9a-z]", re.IGNORECASE)


def _hits(text: str) -> list[str]:
    return [
        f"{lineno}: {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if MODEL_ID.search(line)
    ]


def test_pattern_catches_known_hardcoded_defaults() -> None:
    """樣式本身要抓得到曾經寫死的預設值，避免樣式寫錯讓本測試永遠綠。"""
    for literal in (
        '"gemini-2.0-flash"',
        "'gemini-2.0-flash-exp'",
        '"gemini-1.5-flash"',
        'useState<string>("gemini-2.5-flash")',
        '"claude-4.6-sonnet"',
        '"default": "gemini-2.0-flash-exp",',
    ):
        assert _hits(literal), literal
    assert not _hits('baseModel: ""')
    assert not _hits("const ToukaGemini = 1")


def test_guarded_files_have_no_hardcoded_model_ids() -> None:
    violations: list[str] = []
    for rel in GUARDED_FILES:
        path = PROJECT_ROOT / rel
        assert path.is_file(), f"受保護的檔案不存在（改名時請同步更新清單）：{rel}"
        violations += [f"{rel}:{hit}" for hit in _hits(path.read_text(encoding="utf-8"))]
    assert not violations, "選模型的地方不得寫死模型 ID（ADR-0250）：\n" + "\n".join(violations)
