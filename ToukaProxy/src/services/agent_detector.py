"""對話目標代理人偵測器 (Agent Target Detector).

負責分析對話上下文或模型名稱，動態偵測並歸一化目標代理人 ID (Identity Unification)。
"""

import logging
from typing import Any

from src.utils import config_utils

logger = logging.getLogger(__name__)


def detect_target_agent(messages: list[Any], model_name: str = "") -> str | None:
    """從對話內容或模型名稱中動態偵測目標 Agent (SSOT 歸一化版)"""
    # 1. 載入註冊表並建立歸一化映射表
    registry = config_utils.read_config_json("agents_registry.json")
    if not registry:
        # 備用保底（僅用於測試環境或初始化，不含真實 Agent 名稱）
        registry = {"Alice": ["Ember", "Star"], "Bob": ["Wave"], "Charlie": ["Charlie"]}

    # 建立歸一化字典 (例如: {"alice": "Alice", "bob": "Bob"})
    norm_map: dict[str, str] = {}
    for agent_id, keywords in registry.items():
        # ID 本身也是關鍵字
        norm_map[agent_id.lower()] = agent_id
        for k in keywords:
            norm_map[k.lower()] = agent_id

    # 2. 優先從模型名稱識別
    if model_name:
        clean_model = model_name.split(":")[-1]  # 處理 toukasoul: 前綴
        low_model = clean_model.lower()

        # 檢查是否為 toukasoulXXX 格式
        if low_model.startswith("toukasoul") and len(low_model) > 9:
            extracted_id = low_model[9:]
            if extracted_id in norm_map:
                return norm_map[extracted_id]
            return None

        # 檢查是否精確匹配 Agent ID 或別名
        if low_model in norm_map:
            return norm_map[low_model]

    # 3. 從對話內容偵測
    context_parts: list[str] = []
    current_length: int = 0

    for m in reversed(messages):
        role = getattr(m, "role", "") if not isinstance(m, dict) else m.get("role", "")
        if role in ["user", "system", "assistant"]:
            content = (
                getattr(m, "content", "")
                if not isinstance(m, dict)
                else m.get("content", "")
            )

            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # 串聯所有文字型區塊
                text = " ".join(
                    [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                )
            elif isinstance(content, dict):
                text = content.get("text", "")

            if text:
                context_parts.append(text.lower())
                current_length += len(text)
            if current_length > 2000:
                break

    full_text = "".join(context_parts)

    # 計分制
    scores = dict.fromkeys(registry.keys(), 0)
    for agent_id, keywords in registry.items():
        # 檢查 ID 本身是否出現在對話中 (不分大小寫)
        scores[agent_id] += full_text.count(agent_id.lower())
        for k in keywords:
            scores[agent_id] += full_text.count(k.lower())

    best_agent_id = None
    highest_score = 0
    for agent_id, score in scores.items():
        if score > highest_score:
            highest_score = score
            best_agent_id = agent_id

    return best_agent_id
