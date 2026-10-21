"""ToukaMemory e5 前綴邏輯單元測試（vector._format_for_embedding）。

e5 系列（multilingual-e5-*）需要非對稱前綴：文件/事實加 ``passage: ``、查詢加
``query: ``；其他模型（如 all-MiniLM）原文不變，確保切換模型時行為向後相容。
"""
from __future__ import annotations

from memory_server.vector import _format_for_embedding


def test_e5_model_adds_asymmetric_prefix():
    m = "intfloat/multilingual-e5-base"
    assert _format_for_embedding(m, "今天天氣很好", is_query=True) == "query: 今天天氣很好"
    assert _format_for_embedding(m, "今天天氣很好", is_query=False) == "passage: 今天天氣很好"


def test_non_e5_model_no_prefix():
    assert _format_for_embedding("all-MiniLM-L6-v2", "hello", is_query=True) == "hello"
    assert _format_for_embedding("all-MiniLM-L6-v2", "hello", is_query=False) == "hello"


def test_empty_model_name_no_prefix():
    assert _format_for_embedding("", "x", is_query=True) == "x"
