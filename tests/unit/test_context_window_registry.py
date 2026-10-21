"""Unit tests for context_window_registry (Phase A — context 量測分母 SSOT).

以 mock 隔離 config 讀取，驗證內建表、1M 標記、最長子字串匹配、JSON 覆蓋、
未知 fallback 與讀檔失敗降級。
"""
from __future__ import annotations

from unittest.mock import patch

from src.services.models.context_window_registry import (
    _DEFAULT_WINDOW,
    get_context_window,
)

_PATCH_TARGET = "src.utils.config_utils.read_config_json"


# ── 內建表（無 JSON 覆蓋）────────────────────────────────────────────────────


def test_builtin_claude():
    with patch(_PATCH_TARGET, return_value=None):
        assert get_context_window("claude-opus-4-8") == 200_000


def test_one_million_marker_overrides_substring():
    with patch(_PATCH_TARGET, return_value=None):
        # [1m] 標記應勝過 claude 子字串的 200K。
        assert get_context_window("claude-opus-4-8[1m]") == 1_000_000


def test_builtin_gemini():
    with patch(_PATCH_TARGET, return_value=None):
        assert get_context_window("gemini-2.5-flash") == 1_048_576


def test_longest_substring_wins():
    with patch(_PATCH_TARGET, return_value=None):
        # gpt-4o-mini 應匹配 gpt-4o(128K) 而非 gpt-4(8K)。
        assert get_context_window("gpt-4o-mini") == 128_000


def test_unknown_model_returns_default():
    with patch(_PATCH_TARGET, return_value=None):
        assert get_context_window("totally-unknown-model-xyz") == _DEFAULT_WINDOW
        assert _DEFAULT_WINDOW == 128_000


def test_none_and_empty_return_default():
    with patch(_PATCH_TARGET, return_value=None):
        assert get_context_window(None) == _DEFAULT_WINDOW
        assert get_context_window("") == _DEFAULT_WINDOW
        assert get_context_window("   ") == _DEFAULT_WINDOW


# ── JSON 覆蓋 ────────────────────────────────────────────────────────────────


def test_json_override_exact_beats_builtin():
    cfg = {"_default": 128000, "windows": {"claude-opus-4-8": 12345}}
    with patch(_PATCH_TARGET, return_value=cfg):
        assert get_context_window("claude-opus-4-8") == 12345


def test_json_custom_default_for_unknown():
    cfg = {"_default": 64000, "windows": {}}
    with patch(_PATCH_TARGET, return_value=cfg):
        assert get_context_window("unknown-model") == 64000


def test_json_invalid_values_ignored():
    # 非正數／非數值的覆蓋應被忽略，退回內建表。
    cfg = {"windows": {"claude-opus-4-8": -1, "gemini": "huge"}}
    with patch(_PATCH_TARGET, return_value=cfg):
        assert get_context_window("claude-opus-4-8") == 200_000
        assert get_context_window("gemini-2.5-flash") == 1_048_576


# ── 降級 ────────────────────────────────────────────────────────────────────


def test_read_failure_degrades_to_builtin():
    with patch(_PATCH_TARGET, side_effect=OSError("disk error")):
        # 讀檔失敗不可拋例外，仍回內建表值。
        assert get_context_window("claude-opus-4-8") == 200_000
        assert get_context_window("unknown") == _DEFAULT_WINDOW


# ── 1M 標記的另外兩種寫法（A2-①：先前只測 [1m]）──────────────────────────────


def test_one_million_alternate_markers():
    with patch(_PATCH_TARGET, return_value=None):
        assert get_context_window("custom-model-1m") == 1_000_000  # -1m 結尾
        assert get_context_window("foo:1m") == 1_000_000  # :1m 中綴


# ── override 子字串匹配優先於內建（A2-②：表順序 (overrides, builtin)）──────────


def test_json_override_substring_beats_builtin():
    cfg = {"windows": {"myorg-llm": 314159}}
    with patch(_PATCH_TARGET, return_value=cfg):
        # 'myorg-llm' 是 'myorg-llm-v2' 的子字串 → 走 override 而非落 default。
        assert get_context_window("myorg-llm-v2") == 314159


# ── JSON key 大小寫正規化（A2-③：str(k).lower()）─────────────────────────────


def test_json_override_key_is_case_insensitive():
    cfg = {"windows": {"CLAUDE-OPUS-4-8": 55555}}
    with patch(_PATCH_TARGET, return_value=cfg):
        assert get_context_window("claude-opus-4-8") == 55555


# ── _default 數值驗證（A2-④：raw_default > 0）─────────────────────────────────


def test_invalid_default_falls_back_to_128k():
    with patch(_PATCH_TARGET, return_value={"_default": 0, "windows": {}}):
        # 非正數 _default 應被忽略，退回內建 128K 預設。
        assert get_context_window("unknown-xyz") == 128_000


# ── 最長子字串「最具體者勝」用真實型號加強（A2-⑤）───────────────────────────────


def test_longest_substring_picks_most_specific_gemini():
    with patch(_PATCH_TARGET, return_value=None):
        # gemini-1.5-pro(2M) 應勝過 gemini-1.5 / gemini（各 1.05M）。
        assert get_context_window("gemini-1.5-pro-002") == 2_097_152


# ── 內建表代表值（A2-⑥：各 provider 抽一釘死，殺資料值變異）─────────────────────


def test_builtin_table_representative_values():
    with patch(_PATCH_TARGET, return_value=None):
        expected = {
            "gpt-4": 8_192,  # 注意：gpt-4 精確值偏小，誤值影響大
            "o1": 200_000,
            "grok-3": 131_072,
            "grok-4": 256_000,  # grok-4 為 grok 家唯一 256K
        }
        for model, window in expected.items():
            assert get_context_window(model) == window, f"{model} 內建值漂移"
