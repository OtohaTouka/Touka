"""Touka 遙測與效能監控服務 (Telemetry & Monitoring).

本模組負責追蹤系統效能、Token 消耗、計算湧現指標 (E-Index) 並進行稽核紀錄。
所有的數據持久化均遵循 MEMORY_APP_DATA_DIR 導航之 SSOT 原則。
"""

import logging
import math
from pathlib import Path

from src.config import get_app_data_dir  # noqa: TID251 - Whitelisted for legacy background tasks.
from src.models.identity import AgentIdentity
from src.services.providers import pricing

from ..utils.time_utils import (
    format_epoch_to_taipei,
    get_current_epoch,
    get_current_taipei_iso,
)

logger = logging.getLogger(__name__)


def calculate_shannon_entropy(text: str) -> float:
    """計算輸入文本的香農熵 (Shannon Entropy)。"""
    if not text:
        return 0.0
    probabilities = [text.count(c) / len(text) for c in set(text)]
    entropy = -sum(p * math.log2(p) for p in probabilities)
    return entropy


def calculate_emergence_index(
    memory_count: int, avg_memory_score: float, gen_time: float, input_text: str
) -> float:
    """計算湧現指標 (E-Index)。
    公式: E = ((Memory_Load * Score) * Pure_Gen_Time) / Input_Entropy
    """
    input_entropy = calculate_shannon_entropy(input_text)
    if input_entropy == 0:
        input_entropy = 1.0  # 避免除以零

    # 防止 gen_time 過小導致指數失效
    effective_gen_time = max(gen_time, 0.1)

    e_index = ((memory_count * avg_memory_score) * effective_gen_time) / input_entropy
    return round(e_index, 4)


def estimate_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    *,
    model: str | None = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """估算一段用量的美元成本（SSOT：費率查 pricing registry，per-model）。

    費率來源：``config/model_pricing.json`` 覆蓋 → 內建表 → 保守 fallback（見 ``pricing``）。
    ``cache_*`` 未在費率表設定時沿用 input 價（保持重構前行為）。

    Args:
        prompt_tokens: 輸入 token 數。
        completion_tokens: 輸出 token 數。
        model: 模型 ID（決定費率；None／未知 → 保守 fallback）。
        cache_read_tokens: 快取讀取 token 數（選填）。
        cache_write_tokens: 快取寫入 token 數（選填）。

    Returns:
        float: 估算成本（USD，四捨五入至 6 位）。
    """
    rate = pricing.get_pricing(model)
    return rate.cost_usd(
        prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens
    )


def _usage_counts(usage_dict: dict) -> tuple[int, int, int, int]:
    """（未快取輸入, 輸出, 快取讀, 快取寫）；舊鍵 input/output_tokens 照舊相容。"""
    return (
        usage_dict.get("prompt_tokens", usage_dict.get("input_tokens", 0)),
        usage_dict.get("completion_tokens", usage_dict.get("output_tokens", 0)),
        usage_dict.get("cache_read_input_tokens", 0),
        usage_dict.get("cache_creation_input_tokens", 0),
    )


def usage_cost_usd(usage_dict: dict, *, model: str | None = None) -> float:
    """一個請求的四欄用量 dict 的成本，含快取讀寫（ADR-0244）。"""
    pt, ct, cr, cw = _usage_counts(usage_dict)
    return estimate_cost_usd(pt, ct, model=model, cache_read_tokens=cr, cache_write_tokens=cw)


def is_long_prompt(usage_dict: dict, *, model: str | None = None) -> bool:
    """這一個請求是否落在長提示檔（r55：Haiku 5.5 提示長度含快取讀寫超過門檻）。"""
    pt, _, cr, cw = _usage_counts(usage_dict)
    rate = pricing.get_pricing(model)
    return rate.for_prompt(pt + cr + cw) is not rate


def run_cost_usd(totals: dict, long_totals: dict, *, model: str | None = None) -> float:
    """整次 run 金額：長檔那幾輪的加總照長檔費率、其餘照基本費率，各算一次再相加。

    不拿多輪加總去選檔（r55）。沒分檔的型號 ``long_totals`` 永遠是空的，結果等於
    ``usage_cost_usd(totals)``。
    """
    rate = pricing.get_pricing(model)
    short = {key: value - long_totals.get(key, 0) for key, value in totals.items()}
    cost = rate.flat_cost_usd(*_usage_counts(short))
    if long_totals:
        long_rate = rate.long_prompt[1] if rate.long_prompt is not None else rate
        cost += long_rate.flat_cost_usd(*_usage_counts(long_totals))
    return round(cost, 6)


def accumulate_token_usage(
    identity: AgentIdentity | str,
    provider: str,
    usage_dict: dict,
    e_index: float | None = None,
    *,
    model: str | None = None,
):
    """累計並持久化 Token 消耗數據 (Identity SSOT).

    ``e_index`` 有值時才記一筆 E-Index 歷史：react_core 每輪入帳不帶，整次 run 結束時
    以空用量帶一次（ADR-0244）。
    """
    agent_id = identity.agent_id if isinstance(identity, AgentIdentity) else identity
    try:
        from src.services.resource_manager import resource_manager
        stats_dir = resource_manager.get_stats_path(identity) if isinstance(identity, AgentIdentity) else None
        
        if not stats_dir:
            # 如果是背景系統任務且無身分，回傳全域目錄 (暫時相容)
            stats_dir = Path(get_app_data_dir()) / "stats"
            resource_manager.make_dir(stats_dir)

        file_path = Path(stats_dir) / "token_usage.json"

        current_stats = {}
        try:
            data = resource_manager.read_json(file_path)
            if data:
                current_stats = data
        except Exception:
            current_stats = {}

        if provider not in current_stats:
            current_stats[provider] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_cost_usd": 0.0,
                "e_index_history": [],
            }

        p_stats = current_stats[provider]
        # Ensure keys exist for older stat files
        for key in [
            "prompt_tokens",
            "completion_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ]:
            if key not in p_stats:
                p_stats[key] = 0

        this_pt = usage_dict.get("prompt_tokens", usage_dict.get("input_tokens", 0))
        this_ct = usage_dict.get(
            "completion_tokens", usage_dict.get("output_tokens", 0)
        )
        this_cr = usage_dict.get("cache_read_input_tokens", 0)
        this_cw = usage_dict.get("cache_creation_input_tokens", 0)
        p_stats["prompt_tokens"] += this_pt
        p_stats["completion_tokens"] += this_ct
        p_stats["cache_read_input_tokens"] += this_cr
        p_stats["cache_creation_input_tokens"] += this_cw

        # 成本「逐回合增量累計」（ssot-1）：每回合以當回合 token × 該模型費率算出後累加，
        # 不再從累計桶用單一費率重算。如此（a）同供應商多模型不互相污染、
        # （b）日後改費率不回溯重算歷史。cache token 一併計入（ssot-2；非獨立計費的供應商
        # 因費率 None 不另計）。SSOT：estimate_cost_usd。
        turn_cost = estimate_cost_usd(
            this_pt,
            this_ct,
            model=model,
            cache_read_tokens=this_cr,
            cache_write_tokens=this_cw,
        )
        p_stats["total_cost_usd"] = round(
            float(p_stats.get("total_cost_usd", 0.0)) + turn_cost, 6
        )

        # 記錄 E-Index 歷史 (保留最近 50 筆)
        if "e_index_history" not in p_stats:
            p_stats["e_index_history"] = []
        if e_index is not None:
            history_entry = {"timestamp": get_current_taipei_iso(), "e_index": e_index}
            p_stats["e_index_history"].append(history_entry)
            p_stats["e_index_history"] = p_stats["e_index_history"][-50:]

        resource_manager.make_dir(Path(file_path).parent)
        resource_manager.write_json(file_path, current_stats, indent=2)
    except Exception as e:
        logger.error(f"❌ Failed to accumulate token usage for {agent_id}: {e}")


def log_usage_audit(
    identity: AgentIdentity | str,
    request_id: str,
    provider: str,
    latency_info: dict,
    usage_dict: dict,
    *,
    model: str | None = None,
    cost_usd: float | None = None,
):
    """將效能與成本數據寫入 CSV 稽核日誌 (Identity SSOT).

    ``cost_usd`` 有值就照寫（整次 run 由呼叫端按檔算好，r55）；None 照舊由用量算。
    """
    agent_id = identity.agent_id if isinstance(identity, AgentIdentity) else identity
    try:
        from src.services.resource_manager import resource_manager
        stats_dir = resource_manager.get_stats_path(identity) if isinstance(identity, AgentIdentity) else None
        
        if not stats_dir:
            stats_dir = Path(get_app_data_dir()) / "stats"
            resource_manager.make_dir(stats_dir)

        csv_file = Path(stats_dir) / "usage_audit.csv"

        headers = [
            "timestamp", "request_id", "agent_id", "provider",
            "total_ms", "local_ms", "network_ms", "pure_gen_ms",
            "e_index", "input_tokens", "output_tokens", "cost_usd", "rate_source",
        ]

        in_tokens = usage_dict.get("prompt_tokens", usage_dict.get("input_tokens", 0))
        out_tokens = usage_dict.get("completion_tokens", usage_dict.get("output_tokens", 0))
        # 殺重複硬編碼（ssot-4）：一律走 estimate_cost_usd（SSOT）。model 預設 None，
        # 故簽章變更原子化——絕不會因 model 未定義而拋例外、靜默漏記稽核列。
        # cost_usd 含快取讀寫；欄位不變（ADR-0244 決定 A5、P20）。
        cost = usage_cost_usd(usage_dict, model=model) if cost_usd is None else cost_usd
        # rate_source（security-3）：標記成本來源（override/builtin 或 fallback 粗估），
        # 讓 append-only 稽核帳本事後可分辨哪些成本可信。
        rate_source = pricing.resolve(model)[1]

        row = [
            format_epoch_to_taipei(get_current_epoch())[0],
            request_id, agent_id, provider,
            latency_info.get("total_ms", 0), latency_info.get("local_ms", 0),
            latency_info.get("network_ms", 0), latency_info.get("pure_gen_ms", 0),
            latency_info.get("e_index", 0.0), in_tokens, out_tokens, f"{cost:.6f}", rate_source,
        ]
        
        resource_manager.append_csv_row(csv_file, row, headers=headers)
    except Exception as e:
        logger.error(f"Failed to write usage audit: {e}")
