"""MCP curated 條目 env 哨兵解析（SSOT）。

curated MCP 條目可用哨兵代表 Touka 受管路徑，於 **install 時**與**每次連線啟動時**皆解析為
絕對路徑（故 server_config.json 可保存可攜的哨兵、由各機在啟動時還原）：

- ``@TOUKA_OUTPUT@``  → 共用文件輸出暫存夾（``app_data/temp/docgen_output``）。
- ``@TOUKA_APPDATA@`` → app_data 根。作 mcp-docgen 讀取邊界 **backstop**：即使 per-agent
  ``vault_jail_arg`` 政策哪天漂移掉，docgen 仍只能在 app_data 內（而非全機）讀寫。

env 值與啟動引數（args）皆適用同一組哨兵（ADR-0186：官方 filesystem 以 args 哨兵帶
允許目錄）。純路徑運算、無 I/O（呼叫端自行視需要建夾）。
"""

from __future__ import annotations

from pathlib import Path

from src.config import get_app_data_dir

OUTPUT_DIR_SENTINEL = "@TOUKA_OUTPUT@"
APPDATA_SENTINEL = "@TOUKA_APPDATA@"


def resolve_env_sentinel(value: str) -> str:
    """單一 env 值：哨兵 → 絕對路徑；非哨兵原樣回傳。"""
    if value == OUTPUT_DIR_SENTINEL:
        return str(Path(get_app_data_dir()) / "temp" / "docgen_output")
    if value == APPDATA_SENTINEL:
        return str(Path(get_app_data_dir()))
    return value


def resolve_env_sentinels(env: dict[str, str] | None) -> dict[str, str]:
    """整個 env dict 的哨兵解析（None → 空 dict）。"""
    return {k: resolve_env_sentinel(v) for k, v in (env or {}).items()}


def resolve_args_sentinels(args: list[str] | None) -> list[str]:
    """啟動引數清單的哨兵解析（None → 空 list；逐元素委派 ``resolve_env_sentinel``）。"""
    return [resolve_env_sentinel(a) for a in (args or [])]
