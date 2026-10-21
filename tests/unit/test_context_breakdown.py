"""Unit tests for context_breakdown（Phase B / ADR-0092）。

驗證 usage_pct 計算、XML 區塊分解＋authoritative 校正、巢狀歸類、降級與
token_usage_event 擴充欄位。
"""
from __future__ import annotations

from src.adapters import tool_schema
from src.services.context_breakdown import _system_to_text, compute_context_breakdown
from src.utils.progress_events import token_usage_event

_MODEL = "claude-opus-4-8"  # window 200_000


def test_cached_system_block_list_flattens_same_as_str():
    """P4b：to_cached_system 產生的快取 block list 經 _system_to_text 攤平＝原字串，
    且 compute_context_breakdown 分解不受快取斷點影響（遙測相容）。"""
    text = "<persona>Touka</persona>\n<capabilities>x</capabilities>"
    cached = tool_schema.to_cached_system(text)
    assert _system_to_text(cached) == text
    a = compute_context_breakdown(text, None, None, _MODEL, 1000)
    b = compute_context_breakdown(cached, None, None, _MODEL, 1000)
    assert a["breakdown"] == b["breakdown"]


def test_cached_system_split_breakdown_stays_sensible():
    """P4c：穩定前綴細分（兩個 block）後 context breakdown 仍合理——主要內容分類
    （persona／mission_awareness）不變、總量近似。

    註：API 端 Anthropic 直接串接 block（無分隔符）＝送出文字完全正確；僅遙測
    `_system_to_text` 攤平時多一個換行，可能多出可忽略的 system_other（~0 token）。
    """
    text = "<persona>Touka</persona>\n<mission_awareness>2/5</mission_awareness>"
    cached = tool_schema.to_cached_system(text, volatile_tags=("mission_awareness",))
    assert len(cached) == 2  # 切成穩定前綴＋波動後綴
    a = compute_context_breakdown(text, None, None, _MODEL, 1000)
    b = compute_context_breakdown(cached, None, None, _MODEL, 1000)
    assert {"persona", "mission_awareness"} <= set(a["breakdown"])
    assert {"persona", "mission_awareness"} <= set(b["breakdown"])
    assert abs(a["total_tokens"] - b["total_tokens"]) <= 2  # 差異僅攤平多的一個換行


def test_reorder_preserves_breakdown_categories():
    """P4d：中介層順序重排只搬動整塊區塊——context breakdown 逐 tag 解析與位置無關，
    故各區塊分類與 token 計數不變（僅空白殘留可能差 ~0 token）。"""
    text = (
        "<persona>Touka soul</persona>\n\n"
        "<conversation_summary>trimmed chat here</conversation_summary>\n"
        "<tool_availability>filtered note</tool_availability>\n"
        "<mission_awareness>progress 2/5</mission_awareness>\n"
    )
    volatile = ("mission_awareness", "conversation_summary")
    reordered = tool_schema.reorder_system_stable_first(text, volatile_tags=volatile)
    a = compute_context_breakdown(text, None, None, _MODEL, 1000)
    b = compute_context_breakdown(reordered, None, None, _MODEL, 1000)
    # 重排後各命名區塊分類完全一致（僅相對順序改變）
    named_a = {k: v for k, v in a["breakdown"].items() if k != "system_other"}
    named_b = {k: v for k, v in b["breakdown"].items() if k != "system_other"}
    assert named_a == named_b
    assert abs(a["total_tokens"] - b["total_tokens"]) <= 2


# ── usage_pct ────────────────────────────────────────────────────────────────


def test_usage_pct_from_input_tokens():
    out = compute_context_breakdown(None, None, None, _MODEL, 100_000)
    assert out["window"] == 200_000
    assert out["usage_pct"] == 50.0
    assert out["total_tokens"] == 100_000
    assert out["breakdown"] == {}


def test_usage_pct_capped_at_100():
    out = compute_context_breakdown(None, None, None, _MODEL, 999_999_999)
    assert out["usage_pct"] == 100.0


def test_one_million_window_for_1m_model():
    out = compute_context_breakdown(None, None, None, "claude-opus-4-8[1m]", 100_000)
    assert out["window"] == 1_000_000
    assert out["usage_pct"] == 10.0


# ── 分解 ─────────────────────────────────────────────────────────────────────


def test_breakdown_parses_xml_blocks_and_scales():
    system = "<persona>\n我是花同\n</persona>\n<capabilities>\n<skill>x</skill>\n</capabilities>"
    tools = [{"name": "research", "input_schema": {}}]
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "嗨"},
    ]
    out = compute_context_breakdown(system, tools, messages, _MODEL, 5000)
    bd = out["breakdown"]
    assert "persona" in bd
    assert "capabilities" in bd
    assert "tools" in bd
    assert "conversation" in bd
    # 校正後各塊加總 ≈ authoritative total（容許四捨五入誤差）。
    assert abs(sum(bd.values()) - 5000) <= len(bd)


def test_nested_project_counted_as_project_not_persona():
    # project 巢狀於 persona（soul 吸收先前 system 內容）→ 應歸 project_context。
    system = "<persona>\n靈魂\n<project_context>\nPROJ\n</project_context>\n</persona>"
    out = compute_context_breakdown(system, None, None, _MODEL, None)
    bd = out["breakdown"]
    assert "project_context" in bd
    assert "persona" in bd


def test_no_authoritative_uses_local_total():
    system = "<persona>\nhello world this is some text\n</persona>"
    out = compute_context_breakdown(system, None, None, _MODEL, None)
    assert out["total_tokens"] > 0
    assert out["usage_pct"] >= 0


def test_never_raises_on_garbage():
    out = compute_context_breakdown(12345, {"weird": object()}, "not-a-list", _MODEL, "bad")
    assert "usage_pct" in out
    assert isinstance(out["breakdown"], dict)


# ── helpers ──────────────────────────────────────────────────────────────────


def test_system_to_text_handles_variants():
    assert _system_to_text(None) == ""
    assert _system_to_text("abc") == "abc"
    assert "x" in _system_to_text([{"type": "text", "text": "x"}, "y"])


# ── token_usage_event 擴充 ────────────────────────────────────────────────────


def test_token_usage_event_without_extras():
    ev = token_usage_event(100, 0.5)
    assert ev["kind"] == "token_usage"
    assert "usage_pct" not in ev
    assert "context_breakdown" not in ev


def test_token_usage_event_with_extras():
    ev = token_usage_event(100, 0.5, usage_pct=42.0, context_breakdown={"breakdown": {}})
    assert ev["usage_pct"] == 42.0
    assert ev["context_breakdown"] == {"breakdown": {}}
