"""ADR-0069 Phase C（SCAFFOLD）單元測試。

涵蓋：決定性生成（ast-valid、MCPServer/entrypoint、每工具一個 @mcp.tool()、參數型別）、
驗證（識別字/關鍵字/重複/空/保留名/路徑安全 server 名）、**注入防護**（惡意工具名被拒、
惡意描述只成惰性字面值、引號/換行可 compile）、寫檔到 config/mcp_servers/<name>/server.py、
回傳 McpSpec(command=sys.executable)、不覆寫既有、ast 兜底、不動既有產物。

沙箱由 tests/conftest.py 提供（TOUKA_CONFIG_DIR → tmp）；唯一臨時 server 名、零接觸真實資料。
"""
from __future__ import annotations

import ast
import sys
from unittest.mock import patch

from src.config import get_config_dir
from src.services.creation.models import McpScaffoldSpec, ParamSpec, ToolSpec
from src.services.creation.scaffold import (
    generate_fastmcp_source,
    mcp_scaffolder,
    validate_scaffold_spec,
)
from src.services.resource_manager import resource_manager

_PATCH_GENERATE = "src.services.creation.scaffold.generate_fastmcp_source"


def _server_py_path(name: str):
    return get_config_dir() / "mcp_servers" / name / "server.py"


# --- 生成（純、決定性）---


def test_generate_produces_ast_valid_source():
    spec = McpScaffoldSpec(
        name="gen-srv",
        tools=[
            ToolSpec(
                name="get_forecast",
                description="Get forecast.",
                params=[ParamSpec(name="city"), ParamSpec(name="days", type="int")],
            ),
            ToolSpec(name="alerts", description="Active alerts."),
        ],
    )
    ast.parse(generate_fastmcp_source(spec))  # raises on invalid


def test_generate_contains_mcpserver_and_entrypoint():
    src = generate_fastmcp_source(
        McpScaffoldSpec(name="Gen Srv", tools=[ToolSpec(name="ping")])
    )
    assert "from mcp.server import MCPServer" in src
    assert "mcp = MCPServer('Gen Srv', version='2.0.0')" in src
    assert 'if __name__ == "__main__":' in src
    assert "mcp.run()" in src


def test_generate_one_tool_decorator_per_tool():
    src = generate_fastmcp_source(
        McpScaffoldSpec(
            name="gen-srv",
            tools=[ToolSpec(name="a"), ToolSpec(name="b"), ToolSpec(name="c")],
        )
    )
    assert src.count("\n@mcp.tool()\n") == 3  # 獨立成行的裝飾器（docstring 內的提及不算）
    assert "def a(" in src and "def b(" in src and "def c(" in src


def test_generate_param_signature_and_types():
    src = generate_fastmcp_source(
        McpScaffoldSpec(
            name="gen-srv",
            tools=[
                ToolSpec(
                    name="f",
                    params=[
                        ParamSpec(name="x"),
                        ParamSpec(name="n", type="int"),
                        ParamSpec(name="w", type="weird"),  # 未知 → str
                    ],
                )
            ],
        )
    )
    assert "def f(x: str, n: int, w: str) -> str:" in src


def test_generate_is_deterministic():
    spec = McpScaffoldSpec(
        name="gen-srv", tools=[ToolSpec(name="a", description="d"), ToolSpec(name="b")]
    )
    s1 = generate_fastmcp_source(spec)
    s2 = generate_fastmcp_source(spec)
    assert s1 == s2
    assert "import datetime" not in s1 and "import time" not in s1


def test_generate_zero_param_tool():
    src = generate_fastmcp_source(
        McpScaffoldSpec(name="gen-srv", tools=[ToolSpec(name="alerts")])
    )
    assert "def alerts() -> str:" in src


# --- 驗證 ---


def test_validate_rejects_non_identifier_tool_name():
    for bad in ["get-forecast", "123x", "class"]:
        errs = validate_scaffold_spec(
            McpScaffoldSpec(name="srv", tools=[ToolSpec(name=bad)])
        )
        assert errs, f"{bad!r} should be rejected"


def test_validate_rejects_non_identifier_param_name():
    for bad in ["for", "a-b", "1x"]:
        errs = validate_scaffold_spec(
            McpScaffoldSpec(
                name="srv", tools=[ToolSpec(name="f", params=[ParamSpec(name=bad)])]
            )
        )
        assert errs, f"{bad!r} should be rejected"


def test_validate_rejects_duplicate_tool_names():
    errs = validate_scaffold_spec(
        McpScaffoldSpec(name="srv", tools=[ToolSpec(name="ping"), ToolSpec(name="ping")])
    )
    assert any("重複" in e for e in errs)


def test_validate_rejects_empty_tools():
    assert validate_scaffold_spec(McpScaffoldSpec(name="srv", tools=[]))


def test_validate_rejects_reserved_mcp_name():
    assert validate_scaffold_spec(
        McpScaffoldSpec(name="srv", tools=[ToolSpec(name="mcp")])
    )


def test_validate_rejects_unsafe_server_name():
    # 路徑穿越防護：正規化後須為單段 slug
    for bad in ["../../etc", "a/b", "a.b", "-bad", "  "]:
        errs = validate_scaffold_spec(
            McpScaffoldSpec(name=bad, tools=[ToolSpec(name="ping")])
        )
        assert errs, f"{bad!r} should be rejected"


def test_validate_ok_for_clean_spec():
    errs = validate_scaffold_spec(
        McpScaffoldSpec(
            name="Weather Buddy",
            tools=[ToolSpec(name="get_forecast", params=[ParamSpec(name="city")])],
        )
    )
    assert errs == []


# --- 注入防護（核心安全）---


def test_injection_malicious_tool_name_rejected():
    evil = 'x): import os; os.system("rm -rf /")\n@mcp.tool()\ndef y('
    res = mcp_scaffolder.scaffold(
        McpScaffoldSpec(name="inj-srv", tools=[ToolSpec(name=evil)])
    )
    assert res.ok is False
    assert not resource_manager.exists(_server_py_path("inj-srv"))


def test_injection_malicious_description_is_inert_literal():
    evil = '"""\n); import os; os.system("evil")\n#'
    src = generate_fastmcp_source(
        McpScaffoldSpec(
            name="inj2-srv",
            tools=[ToolSpec(name="safe_tool", description=evil)],
        )
    )
    tree = ast.parse(src)  # 仍可解析
    consts = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert any("os.system" in c for c in consts)  # 危險文字只以「資料」存在
    # 程式碼中無對 .system 的呼叫
    bad_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "system"
    ]
    assert bad_calls == []


def test_injection_quotes_newlines_in_description_compile():
    desc = "has \"double\" and 'single' and \n newline and \\ backslash"
    src = generate_fastmcp_source(
        McpScaffoldSpec(name="inj3-srv", tools=[ToolSpec(name="t", description=desc)])
    )
    compile(src, "<generated>", "exec")  # 只編譯、絕不執行


# --- 寫檔 + handoff ---


def test_scaffold_writes_server_py_to_config_path():
    spec = McpScaffoldSpec(name="write-srv", tools=[ToolSpec(name="ping")])
    res = mcp_scaffolder.scaffold(spec)
    assert res.ok is True
    path = _server_py_path("write-srv")
    assert resource_manager.exists(path)
    assert resource_manager.read_file_text(path) == generate_fastmcp_source(spec)


def test_scaffold_returns_mcp_spec_with_sys_executable():
    spec = McpScaffoldSpec(
        name="handoff-srv", description="d", tools=[ToolSpec(name="ping")]
    )
    res = mcp_scaffolder.scaffold(spec)
    assert res.ok is True
    path = _server_py_path("handoff-srv")
    assert res.server_path == str(path)
    assert res.spec is not None
    assert res.spec.command == sys.executable
    assert res.spec.args == [str(path)]
    assert res.spec.name == "handoff-srv"
    assert res.spec.description == "d"


def test_scaffold_refuses_overwrite_existing_file():
    spec = McpScaffoldSpec(name="dup-srv", tools=[ToolSpec(name="ping")])
    assert mcp_scaffolder.scaffold(spec).ok is True
    before = resource_manager.read_file_text(_server_py_path("dup-srv"))
    second = mcp_scaffolder.scaffold(spec)
    assert second.ok is False
    assert any("覆寫" in e or "已存在" in e for e in second.errors)
    assert resource_manager.read_file_text(_server_py_path("dup-srv")) == before


def test_scaffold_refuses_when_name_registered():
    resource_manager.write_json(
        get_config_dir() / "server_config.json",
        {"mcpServers": {"reg-srv": {"command": "uvx", "args": [], "env": {}}}},
    )
    res = mcp_scaffolder.scaffold(
        McpScaffoldSpec(name="reg-srv", tools=[ToolSpec(name="ping")])
    )
    assert res.ok is False
    assert not resource_manager.exists(_server_py_path("reg-srv"))


def test_scaffold_does_not_touch_existing_artifact():
    mcp_scaffolder.scaffold(
        McpScaffoldSpec(name="keep-srv", tools=[ToolSpec(name="ping")])
    )
    before = resource_manager.read_file_text(_server_py_path("keep-srv"))
    mcp_scaffolder.scaffold(
        McpScaffoldSpec(name="other-srv", tools=[ToolSpec(name="pong")])
    )
    after = resource_manager.read_file_text(_server_py_path("keep-srv"))
    assert before == after


def test_scaffold_refuses_unparseable_source():
    spec = McpScaffoldSpec(name="bad-src-srv", tools=[ToolSpec(name="ping")])
    with patch(_PATCH_GENERATE, return_value="def (:"):
        res = mcp_scaffolder.scaffold(spec)
    assert res.ok is False
    assert any("解析" in e for e in res.errors)
    assert not resource_manager.exists(_server_py_path("bad-src-srv"))


def test_scaffold_invalid_spec_short_circuits_before_write():
    res = mcp_scaffolder.scaffold(
        McpScaffoldSpec(name="short-srv", tools=[ToolSpec(name="bad-name")])
    )
    assert res.ok is False
    assert res.errors
    assert not resource_manager.exists(_server_py_path("short-srv"))
