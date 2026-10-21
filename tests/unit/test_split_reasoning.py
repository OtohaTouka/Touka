"""T1（思考分頻，ADR-0152）：split_reasoning 剝離工具的 TDD。

契約：`split_reasoning(text) -> (reasoning, clean)`。
- 剝離開頭的 ``<think>…</think>`` 或 ``<thinking>…</thinking>`` 區塊（含其後空行），
  reasoning＝標籤內文、clean＝其餘正文。
- 未閉合標籤（串流被截斷／模型只吐思考）＝整段視為 reasoning、clean 空字串。
- 無標籤＝原文直通（reasoning 空）。
- 多個連續開頭區塊全數併入 reasoning；正文「中間」出現的標籤不處理（避免誤傷
  使用者引用的字面標籤——僅信任開頭位置＝模型思考前綴的實際型態）。

實際封包佐證（T1-capture, 2026-07-12）：grok-4.5 傳統端點思考走獨立
``message.reasoning_content`` 欄位、content 乾淨——欄位讀取由 handler 處理；
本工具涵蓋「內嵌標籤」型（DeepSeek-R1 蒸餾/Ollama 本地模型等）＝防禦縱深。
"""

from __future__ import annotations

from src.utils.reasoning import split_reasoning


def test_no_tags_passthrough():
    assert split_reasoning("hello world") == ("", "hello world")


def test_empty_and_none_safe():
    assert split_reasoning("") == ("", "")
    assert split_reasoning(None) == ("", "")


def test_think_block_stripped():
    text = "<think>let me compute 17*23</think>\n\n391"
    reasoning, clean = split_reasoning(text)
    assert reasoning == "let me compute 17*23"
    assert clean == "391"


def test_thinking_block_stripped():
    text = "<thinking>\nplan: greet politely\n</thinking>\n\nHello!"
    reasoning, clean = split_reasoning(text)
    assert reasoning == "plan: greet politely"
    assert clean == "Hello!"


def test_unclosed_tag_all_reasoning():
    """未閉合（截斷/純思考回合）＝整段思考、正文空。"""
    text = "<think>this never ends..."
    reasoning, clean = split_reasoning(text)
    assert reasoning == "this never ends..."
    assert clean == ""


def test_multiple_leading_blocks_merged():
    text = "<think>step 1</think>\n<thinking>step 2</thinking>\nanswer"
    reasoning, clean = split_reasoning(text)
    assert "step 1" in reasoning
    assert "step 2" in reasoning
    assert clean == "answer"


def test_mid_text_tag_untouched():
    """正文中段的字面標籤（如使用者引用）不得誤剝。"""
    text = "回答：<think> 標籤的用途是…"
    reasoning, clean = split_reasoning(text)
    assert reasoning == ""
    assert clean == text


def test_leading_whitespace_tolerated():
    text = "  \n<think>x</think>ok"
    reasoning, clean = split_reasoning(text)
    assert reasoning == "x"
    assert clean == "ok"


def test_case_insensitive_tags():
    reasoning, clean = split_reasoning("<THINK>loud</THINK>done")
    assert reasoning == "loud"
    assert clean == "done"
