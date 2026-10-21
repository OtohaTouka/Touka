"""ADR-0069 Phase C — SCAFFOLD：從零決定性生成 MCPServer v2 stub（SRP/SSOT）。

純生成器（``generate_fastmcp_source`` / ``validate_scaffold_spec``，零 IO、決定性、
無時間戳）＋ 編排類別 ``McpScaffolder``（驗證 → 生成 → ``ast`` 兜底 → 經
``resource_manager`` 寫檔 → 產出可餵 ``creation_engine.create('mcp', spec)`` 的 McpSpec）。

注入防護：識別字校驗 ＋ ``repr()`` 轉義所有自由文字 ＋ 型別白名單 ＋ ``ast.parse`` 兜底；
路徑安全：正規化後 server 名須為單段 slug；不覆寫既有產物（檔案已存在或名稱已註冊即拒）。
"""
from __future__ import annotations

import ast
import keyword
import re
import sys
from typing import TYPE_CHECKING

from src.config import get_config_dir
from src.services.creation.models import McpSpec, ScaffoldResult
from src.services.creation.naming import server_name
from src.services.mcp_hub_service import MCPHubService
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from src.services.creation.models import McpScaffoldSpec, ToolSpec

_PY_TYPES = {"str", "int", "float", "bool"}
_RESERVED_NAMES = {"mcp"}
_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MODULE_DOC = (
    "Touka 創建引擎生成的 MCPServer v2 stub（ADR-0069 Phase C）。\n\n"
    "此檔由 McpScaffolder 決定性生成（stub-only、零 LLM）；每個 @mcp.tool() 為佔位實作、"
    "回傳 placeholder，請填入真正邏輯。以 stdio 啟動：python server.py。"
)


def _is_safe_identifier(name: str) -> bool:
    """合法 Python 識別字且非關鍵字。"""
    return bool(name) and name.isidentifier() and not keyword.iskeyword(name)


def _map_type(type_name: str) -> str:
    """型別白名單映射；未知型別降級為 ``str``（永不取用使用者字串於程式碼位置）。"""
    return type_name if type_name in _PY_TYPES else "str"


def validate_scaffold_spec(spec: McpScaffoldSpec) -> list[str]:
    """靜態驗證 scaffold 規格；回傳可行動錯誤清單（空＝通過）。"""
    errors: list[str] = []
    norm = server_name(spec.name)
    if not norm:
        errors.append("server name 不可為空")
    elif not _SAFE_NAME_RE.match(norm):
        errors.append(
            f"server name 正規化後 {norm!r} 含不安全字元（限小寫英數/底線/連字號）"
        )
    if not spec.tools:
        errors.append("至少需宣告一個工具（空 server 無法通過 SMOKE）")
    seen: set[str] = set()
    for tool in spec.tools:
        tname = tool.name
        if (
            not _is_safe_identifier(tname)
            or tname in _RESERVED_NAMES
            or tname.startswith("__")
        ):
            errors.append(f"工具名 {tname!r} 必須為合法 Python 識別字（非關鍵字/保留名/dunder）")
        if tname in seen:
            errors.append(f"工具名 {tname!r} 重複")
        seen.add(tname)
        pseen: set[str] = set()
        for p in tool.params:
            if not _is_safe_identifier(p.name) or p.name == "self":
                errors.append(f"工具 {tname!r} 的參數名 {p.name!r} 不是合法 Python 識別字")
            if p.name in pseen:
                errors.append(f"工具 {tname!r} 的參數名 {p.name!r} 重複")
            pseen.add(p.name)
    return errors


def _render_tool_lines(tool: ToolSpec) -> list[str]:
    """生成單一 ``@mcp.tool()`` stub 的程式碼行（描述以 repr 轉義為 docstring）。"""
    sig = ", ".join(f"{p.name}: {_map_type(p.type)}" for p in tool.params)
    placeholder = f"TODO: implement {tool.name}"
    return [
        "@mcp.tool()",
        f"def {tool.name}({sig}) -> str:",
        f"    {tool.description!r}",
        f"    return {placeholder!r}",
    ]


def generate_fastmcp_source(spec: McpScaffoldSpec) -> str:
    """由規格決定性生成完整 MCPServer v2 原始碼（純函式、無時間戳）。"""
    lines: list[str] = [
        f'"""{_MODULE_DOC}"""',
        "from mcp.server import MCPServer",
        "",
        f"mcp = MCPServer({spec.name!r}, version='2.0.0')",
    ]
    for tool in spec.tools:
        lines.append("")
        lines.append("")
        lines.extend(_render_tool_lines(tool))
    lines.append("")
    lines.append("")
    lines.append('if __name__ == "__main__":')
    lines.append("    mcp.run()")
    return "\n".join(lines) + "\n"


def _degraded_type_warnings(spec: McpScaffoldSpec) -> list[str]:
    """蒐集被降級為 str 的未知參數型別（非錯誤、僅提醒）。"""
    warnings: list[str] = []
    for tool in spec.tools:
        for p in tool.params:
            if p.type not in _PY_TYPES:
                warnings.append(
                    f"工具 {tool.name} 參數 {p.name} 型別 {p.type!r} 不支援，已降級為 str"
                )
    return warnings


class McpScaffolder:
    """從零生成 MCPServer v2 stub 並產出可餵引擎的 McpSpec（SRP）。"""

    def __init__(self, hub_factory: type[MCPHubService] = MCPHubService) -> None:
        """初始化 MCPServer 鷹架生成器，注入 MCPHubService 類別工廠。"""
        self._hub_factory = hub_factory

    def scaffold(self, spec: McpScaffoldSpec) -> ScaffoldResult:
        """驗證 → 生成 → ast 兜底 → 寫檔 → 回傳含 McpSpec 的結果（不覆寫既有產物）。"""
        errors = validate_scaffold_spec(spec)
        if errors:
            return ScaffoldResult(ok=False, errors=errors)
        name = server_name(spec.name)
        target = get_config_dir() / "mcp_servers" / name / "server.py"
        if resource_manager.exists(target):
            return ScaffoldResult(
                ok=False,
                errors=[f"server.py 已存在於 {name}/，創建不可覆寫既有產物"],
            )
        if name in self._hub_factory().get_server_names():
            return ScaffoldResult(
                ok=False,
                errors=[f"MCP server {name!r} 已註冊，創建不可覆寫既有產物"],
            )
        source = generate_fastmcp_source(spec)
        try:
            ast.parse(source)
        except SyntaxError:
            return ScaffoldResult(ok=False, errors=["生成的原始碼無法解析，已拒絕寫入"])
        resource_manager.write_file_text(target, source, make_parents=True)
        handoff = McpSpec(
            name=spec.name,
            command=sys.executable,
            args=[str(target)],
            env={},
            description=spec.description,
            sdk_major=2,
        )
        return ScaffoldResult(
            ok=True,
            server_path=str(target),
            spec=handoff,
            warnings=_degraded_type_warnings(spec),
        )


# 模組級單例（與 creation_engine / eval_runner 一致）。
mcp_scaffolder = McpScaffolder()
