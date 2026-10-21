"""Metrics Utilities for ToukaProxy.

Provides mathematical calculations for entropy and emergence index (E-Index).
"""

import math
from collections import Counter


def calculate_shannon_entropy(text: str) -> float:
    """計算字串的夏農熵 (Shannon Entropy)。"""
    if not text:
        return 0.0

    counts = Counter(text)
    total_len = len(text)

    entropy = 0.0
    for count in counts.values():
        p = count / total_len
        if p > 0:
            entropy -= p * math.log2(p)

    return entropy


def calculate_emergence_index(
    memory_count: int, avg_memory_score: float, gen_time: float, input_text: str
) -> float:
    """湧現指標公式 (E-Index)。

    E = ((Memory_Count * Avg_Score) * Pure_Gen_Time) / Input_Entropy
    """
    entropy = calculate_shannon_entropy(input_text)

    # 避免分母極小
    if entropy < 0.1:
        entropy = 0.1

    # 計算 MemoryAssociation_Load
    # 若無記憶，Load 為 1.0 (基礎值)
    memory_load = (memory_count * avg_memory_score) + 1.0

    # 涌現指數：思考時間與記憶負載成正比，與輸入熵（複雜度）成反比
    e_index = (memory_load * gen_time) / entropy

    return round(e_index, 4)
