"""MCP env 哨兵解析（Phase B backstop）：@TOUKA_APPDATA@/@TOUKA_OUTPUT@ → 絕對路徑。"""
from __future__ import annotations

from src.config import get_app_data_dir
from src.utils.mcp_sentinels import (
    APPDATA_SENTINEL,
    OUTPUT_DIR_SENTINEL,
    resolve_args_sentinels,
    resolve_env_sentinel,
    resolve_env_sentinels,
)


def test_appdata_sentinel_resolves_to_app_data_root():
    assert resolve_env_sentinel(APPDATA_SENTINEL) == str(get_app_data_dir())


def test_output_sentinel_resolves_under_app_data_temp():
    out = resolve_env_sentinel(OUTPUT_DIR_SENTINEL)
    assert out.endswith("docgen_output")
    assert str(get_app_data_dir()) in out


def test_non_sentinel_value_passes_through():
    assert resolve_env_sentinel("/some/literal/path") == "/some/literal/path"


def test_resolve_env_sentinels_dict_and_none():
    out = resolve_env_sentinels(
        {"MCP_DOCGEN_INPUT_DIR": APPDATA_SENTINEL, "X": "literal"}
    )
    assert out["MCP_DOCGEN_INPUT_DIR"] == str(get_app_data_dir())
    assert out["X"] == "literal"
    assert resolve_env_sentinels(None) == {}


def test_resolve_args_sentinels_elementwise():
    """啟動引數清單逐元素解析（ADR-0186）：哨兵還原、非哨兵原樣、None/空清單安全。"""
    out = resolve_args_sentinels(["-y", "some-pkg", APPDATA_SENTINEL])
    assert out == ["-y", "some-pkg", str(get_app_data_dir())]
    assert resolve_args_sentinels([]) == []
    assert resolve_args_sentinels(None) == []
