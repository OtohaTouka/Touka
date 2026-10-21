"""模型 Context Window 規格表 (Context Window Registry).

提供 ``get_context_window(model_id) -> int``：回傳模型的最大 context window
（token 數），作為 context 百分比量測的分母 SSOT。

來源優先序：
  1. ``config/model_context_windows.json`` 的 ``windows`` 覆蓋（人改／fetcher 補）。
  2. 內建硬編規格表 ``_BUILTIN_WINDOWS``（離線可用、code SSOT）。
  3. 保守 fallback（未知模型；預設 128K，可由 JSON 的 ``_default`` 覆蓋）。

比對採「精確 → 1M 標記 → 最長子字串」三層寬鬆匹配，容忍 provider 前綴與
版本尾綴差異。讀檔失敗一律降級至內建表，**永不拋例外**。
"""
from __future__ import annotations

import logging

from src.utils import config_utils

logger = logging.getLogger(__name__)

_CONFIG_FILE_NAME = "model_context_windows.json"

# 使用者拍板：未知模型保守預設 128K（可由 JSON 的 _default 覆蓋）。
_DEFAULT_WINDOW = 128_000

# 內建硬編規格表（code SSOT，離線可用）。鍵為小寫子字串匹配片段，
# 最長匹配優先（具體勝過泛化）。
_BUILTIN_WINDOWS: dict[str, int] = {
    # ── Anthropic Claude ──
    "claude": 200_000,
    "claude-3": 200_000,
    "claude-3-5": 200_000,
    "claude-3-7": 200_000,
    "claude-haiku-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    # ── Google Gemini ──
    "gemini": 1_048_576,
    "gemini-1.5": 1_048_576,
    "gemini-1.5-pro": 2_097_152,
    "gemini-2.0": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    # ── OpenAI ──
    "gpt-3.5": 16_385,
    "gpt-4": 8_192,
    "gpt-4-turbo": 128_000,
    "gpt-4o": 128_000,
    "gpt-4.1": 1_047_576,
    "o1": 200_000,
    "o3": 200_000,
    # ── xAI Grok ──
    "grok": 131_072,
    "grok-2": 131_072,
    "grok-3": 131_072,
    "grok-4": 256_000,
}

# 1M context beta 標記（如 ``claude-opus-4-8[1m]``）顯式優先於子字串匹配。
_ONE_MILLION = 1_000_000


def _load_config() -> tuple[dict[str, int], int]:
    """回傳 ``(windows_override, default_window)``；讀檔失敗回 ``({}, _DEFAULT_WINDOW)``。"""
    default = _DEFAULT_WINDOW
    overrides: dict[str, int] = {}
    try:
        data = config_utils.read_config_json(_CONFIG_FILE_NAME)
        if isinstance(data, dict):
            raw_default = data.get("_default")
            if isinstance(raw_default, (int, float)) and raw_default > 0:
                default = int(raw_default)
            windows = data.get("windows")
            if isinstance(windows, dict):
                overrides = {
                    str(k).lower(): int(v)
                    for k, v in windows.items()
                    if isinstance(v, (int, float)) and v > 0
                }
    except Exception:
        logger.debug(
            "model_context_windows.json not loaded; using builtin table",
            exc_info=True,
        )
    return overrides, default


def _longest_substring_match(mid: str, table: dict[str, int]) -> int | None:
    """回傳 ``table`` 中作為 ``mid`` 子字串、且鍵最長者的值（無則 None）。"""
    best_key: str | None = None
    for key in table:
        if key in mid and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return table[best_key] if best_key is not None else None


def get_context_window(model: str | None) -> int:
    """回傳模型的 context window（token），未知回保守預設（128K）。永不拋例外。

    Args:
        model: 模型名稱字串（如 ``claude-opus-4-8[1m]``）。非身分物件，
            僅作規格表查詢用。
    """
    overrides, default = _load_config()
    if not isinstance(model, str):
        return default
    mid = model.strip().lower()
    if not mid:
        return default

    # 1. 精確匹配（override 優先於內建）。
    if mid in overrides:
        return overrides[mid]
    if mid in _BUILTIN_WINDOWS:
        return _BUILTIN_WINDOWS[mid]

    # 2. 1M context beta 標記顯式優先。
    if "[1m]" in mid or mid.endswith("-1m") or ":1m" in mid:
        return _ONE_MILLION

    # 3. 最長子字串匹配（override 先於 builtin；具體勝過泛化）。
    for table in (overrides, _BUILTIN_WINDOWS):
        match = _longest_substring_match(mid, table)
        if match is not None:
            return match

    # 4. 未知 → 保守 fallback。
    return default
