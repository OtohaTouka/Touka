"""成本量測：以身分的 token_usage.json 前後快照差額取得情境成本。

重用批次0 的逐回合增量累計（estimate_cost_usd，per-model 準確）——計分卡回報的成本即
驅動預算上限與 Token Usage 面板的同一個數字。永不拋例外（讀不到回 0）。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity


def measure(identity: AgentIdentity) -> tuple[float, int]:
    """回傳該身分目前累計 ``(cost_usd, tokens)``；讀不到回 ``(0.0, 0)``。"""
    try:
        path = Path(resource_manager.get_stats_path(identity)) / "token_usage.json"
        data = resource_manager.read_json(path) or {}
    except Exception:
        return 0.0, 0
    cost = 0.0
    tokens = 0
    for prov in data.values():
        if isinstance(prov, dict):
            cost += float(prov.get("total_cost_usd", 0.0) or 0.0)
            tokens += int(prov.get("prompt_tokens", 0) or 0)
            tokens += int(prov.get("completion_tokens", 0) or 0)
    return round(cost, 6), tokens
