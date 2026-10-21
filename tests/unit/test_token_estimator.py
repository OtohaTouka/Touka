"""Unit tests for token_estimator (Phase A — context token 量測基建).

純函式估算器：CJK 感知、保守、永不拋例外。測試以「範圍／相對關係」斷言，
避免綁死啟發式常數。
"""
from __future__ import annotations

from src.utils.token_estimator import estimate_messages_tokens, estimate_tokens

# ── estimate_tokens ──────────────────────────────────────────────────────────


def test_estimate_tokens_empty_and_none():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_plain_english_reasonable():
    est = estimate_tokens("hello world")
    # 11 字 ≈ 個位數 token。
    assert 1 <= est <= 6


def test_estimate_tokens_cjk_reasonable():
    est = estimate_tokens("你好世界")
    # 4 個 CJK 字 ≈ 約 4 token。
    assert 3 <= est <= 8


def test_estimate_tokens_cjk_heavier_than_english_for_same_length():
    # 同字數下 CJK 的 token 數應高於英文（資訊密度較高）。
    cjk = estimate_tokens("一二三四五六七八九十")  # 10 CJK
    eng = estimate_tokens("abcdefghij")  # 10 ASCII
    assert cjk > eng


def test_estimate_tokens_monotonic_in_length():
    assert estimate_tokens("a" * 100) > estimate_tokens("a" * 10)


def test_estimate_tokens_never_negative():
    assert estimate_tokens("   \n\t  ") >= 0


# ── estimate_messages_tokens ─────────────────────────────────────────────────


def test_estimate_messages_empty():
    assert estimate_messages_tokens(None) == 0
    assert estimate_messages_tokens([]) == 0


def test_estimate_messages_sums_content_plus_per_message_overhead():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    expected = estimate_tokens("hello") + estimate_tokens("world") + 2 * 4
    assert estimate_messages_tokens(msgs) == expected


def test_estimate_messages_handles_anthropic_content_blocks():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "分析這段程式碼"},
                {"type": "tool_use", "name": "research", "input": {"q": "x"}},
            ],
        }
    ]
    # 不可拋例外，且應計入文字與工具 block。
    assert estimate_messages_tokens(msgs) > 0


def test_estimate_messages_tolerates_missing_content():
    msgs = [{"role": "user"}, {"role": "assistant", "content": None}]
    # 只剩每則 overhead，仍為正且不拋例外。
    assert estimate_messages_tokens(msgs) == 2 * 4


# ── _is_cjk 全範圍覆蓋（A1-①：先前只打到常用漢字一個範圍）───────────────────────


def test_is_cjk_covers_all_ranges():
    """每個 CJK 範圍的代表字都應被當 1 字/token（10 字=10 token）。

    殺死各範圍的邊界/移除變異。刻意取邊界字（0x3400、0xAC00）以一併殺邊界變異。
    若某範圍被誤關，該字會被當非 CJK（10/3.5≈3 ≠ 10）→ 對應 assert 變紅。
    """
    cjk_samples = {
        "common_ideograph": "中",        # 0x4E2D ∈ 0x4E00–0x9FFF
        "ext_a_lower_bound": "㐀",   # 0x3400 ∈ 0x3400–0x4DBF（下邊界）
        "cjk_punctuation": "、",     # 、 0x3001 ∈ 0x3000–0x303F
        "hiragana": "あ",            # あ 0x3042 ∈ 0x3040–0x30FF
        "katakana": "カ",            # カ 0x30AB ∈ 0x3040–0x30FF
        "fullwidth": "Ａ",           # Ａ 0xFF21 ∈ 0xFF00–0xFFEF
        "hangul_lower_bound": "가",  # 가 0xAC00 ∈ 0xAC00–0xD7AF（下邊界）
    }
    for name, ch in cjk_samples.items():
        # CJK 除數=1.0 → 10 個同範圍字剛好 10 token。
        assert estimate_tokens(ch * 10) == 10, f"{name} 未被歸為 CJK"


# ── _coerce_content block 攤平（A1-②：先前只斷言 >0）──────────────────────────


def test_coerce_text_block_extracted_exactly():
    """content 為 [{'type':'text','text': 中×10}] → 文字 10 token + 每則 4 overhead = 14。"""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "中" * 10}]}]
    assert estimate_messages_tokens(msgs) == 10 + 4


def test_coerce_non_text_block_counts_json_payload():
    """非文字 block（tool_use）以其 JSON 文字長度計入 → 大 payload 估得比空的高。"""
    big = [{"type": "tool_use", "name": "x", "input": {"q": "你好" * 50}}]
    small = [{"type": "tool_use", "name": "x", "input": {}}]
    assert estimate_messages_tokens([{"role": "user", "content": big}]) > estimate_messages_tokens(
        [{"role": "user", "content": small}]
    )


def test_coerce_plain_string_element_in_list():
    """list 內的純字串元素也要計入（isinstance(block, str) 分支）。"""
    msgs = [{"role": "user", "content": ["中" * 10]}]
    assert estimate_messages_tokens(msgs) == 10 + 4


# ── 啟發式除數常數絆線（A1-③：量測 SSOT，誤改/漂移應弄紅測試）──────────────────


def test_divisor_constants_pinned_as_tripwire():
    """量測 SSOT 的除數絆線：誤改 → 此測試紅（提醒這是全 app context% 的刻意決定）。

    retune 時請同步更新此處期望值。
    """
    assert estimate_tokens("中" * 10) == 10  # CJK 除數=1.0（10/1.0）
    assert estimate_tokens("a" * 35) == 10  # 非CJK 除數=3.5（35/3.5）
