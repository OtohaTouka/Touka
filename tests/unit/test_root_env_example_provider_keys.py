"""根目錄 `.env.example` 必須列出 AI 廠商金鑰，而且值留空。

整合模式只讀根目錄 `.env`（禁令 38），首次啟動由根 `.env.example` 複製而來；
範本少了廠商金鑰，使用者就不知道要填在哪裡。值必須留空，否則佔位字串會被當成真金鑰送出。
後端供應商清單（`guard_provider_parity.backend_required_key_names()`）新增一家時，
本測試會要求範本一起補上，防止再漂移。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import guard_provider_parity as gpp  # noqa: E402

ROOT_ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

# 只認沒被註解掉的 `KEY=value` 行：註解掉的鍵不會被載入。
_ACTIVE_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def _active_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in ROOT_ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = _ACTIVE_LINE_RE.match(line.strip())
        if match:
            entries[match.group(1)] = match.group(2).strip()
    return entries


def test_root_env_example_lists_vendor_keys_with_empty_values():
    entries = _active_entries()
    for key in (
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GROK_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "TAVILY_API_KEY",
    ):
        assert key in entries, f"根 .env.example 缺少 {key}"
        assert entries[key] == "", f"根 .env.example 的 {key} 必須留空"


def test_root_env_example_covers_every_backend_provider_key():
    missing = sorted(gpp.backend_required_key_names() - set(_active_entries()))
    assert missing == [], f"根 .env.example 缺少後端供應商金鑰：{missing}"
