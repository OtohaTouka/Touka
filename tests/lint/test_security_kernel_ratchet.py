"""ADR-0239 ratchet：角色檢查與子程序啟動只能經過共用入口。

純重構後，以靜態 AST 掃描防止兩類重複再長回來：

1. ``ToukaProxy/src/routers/`` 不得再定義私有角色檢查函式；router 內的
   ``_require_*`` 名稱只能是指向 ``services/authorization_gateway.py`` 的別名。
2. 直接啟動子程序（``subprocess.run``／``Popen``、``asyncio.create_subprocess_*``、
   ``os.system``）只能出現在 ``services/execution_broker.py`` 與明列的例外。
3. ``os.exec*``／``fork``／``posix_spawn``／``spawn*``、``pty.spawn``／``fork`` 只能出現在
   隔離啟動器 ``sandbox_kernel/launcher.py``（iso-c2）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROUTERS_DIR = ROOT / "ToukaProxy" / "src" / "routers"
GATEWAY = ROOT / "ToukaProxy" / "src" / "services" / "authorization_gateway.py"
BROKER = ROOT / "ToukaProxy" / "src" / "services" / "execution_broker.py"

# 不是角色檢查、刻意保留的 router 私有函式（檔名 → 函式名）。
_ROUTER_HELPER_EXCEPTIONS: dict[str, frozenset[str]] = {
    # 依 MCP server 授權（grant）與是否存在判斷，不是角色檢查。
    "mcp.py": frozenset({"_require_mcp_data_access", "_require_known_mcp_server"}),
}

_PROCESS_SCAN_ROOTS = (
    ROOT / "ToukaProxy" / "src",
    ROOT / "ToukaMemory" / "src",
    ROOT / "ToukaHeartbeat",
    ROOT / "ToukaDispatch",
)

# 允許直接啟動子程序的模組（相對 repo 根）。
_PROCESS_LAUNCH_ALLOWED = frozenset(
    {
        # ADR-0239 單一啟動入口。
        "ToukaProxy/src/services/execution_broker.py",
        # 終止機制本身（Windows taskkill）。
        "ToukaProxy/src/services/process_registry.py",
        # 獨立服務邊界；外部 AI 召喚依 AGENTS.md 使用 _build_child_env 白名單。
        "ToukaDispatch/server.py",
    }
)

_SUBPROCESS_LAUNCHERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
_ASYNCIO_LAUNCHERS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_role_helper_name(name: str) -> bool:
    return name.startswith(("_require_", "require_"))


def _router_files() -> list[Path]:
    return sorted(p for p in ROUTERS_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("router", _router_files(), ids=lambda p: p.name)
def test_routers_define_no_private_role_helpers(router: Path) -> None:
    """Router 只能以別名引用 gateway，不得自行實作角色檢查。"""
    allowed = _ROUTER_HELPER_EXCEPTIONS.get(router.name, frozenset())
    offenders = [
        f"{router.name}:{node.lineno} {node.name}"
        for node in ast.walk(_parse(router))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _is_role_helper_name(node.name)
        and node.name not in allowed
    ]
    assert offenders == [], (
        "Router 定義了私有角色檢查函式；請改用 services/authorization_gateway.py 的共用函式"
        f"（ADR-0239）：{offenders}"
    )


def test_gateway_exports_the_shared_primitives() -> None:
    names = {
        node.name for node in _parse(GATEWAY).body if isinstance(node, ast.FunctionDef)
    }
    assert {
        "require_privileged",
        "require_owner_or_admin",
        "require_admin_authority",
        "require_authenticated",
        "require_admin_human",
    } <= names


def _process_launches(tree: ast.Module) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"subprocess", "asyncio"}:
            banned = _SUBPROCESS_LAUNCHERS if node.module == "subprocess" else _ASYNCIO_LAUNCHERS
            found.extend(
                (node.lineno, f"from {node.module} import {alias.name}")
                for alias in node.names
                if alias.name in banned
            )
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name):
            continue
        attr = node.func.attr
        if (
            (owner.id == "subprocess" and attr in _SUBPROCESS_LAUNCHERS)
            or (owner.id == "asyncio" and attr in _ASYNCIO_LAUNCHERS)
            or (owner.id == "os" and attr == "system")
        ):
            found.append((node.lineno, f"{owner.id}.{attr}"))
    return found


def _product_python_files() -> list[Path]:
    files: list[Path] = []
    for scan_root in _PROCESS_SCAN_ROOTS:
        files.extend(
            p
            for p in scan_root.rglob("*.py")
            if not {"tests", "__pycache__", ".venv", "node_modules"} & set(p.parts)
        )
    return sorted(files)


def test_process_launches_go_through_execution_broker() -> None:
    """直接啟動子程序只能出現在 broker 與明列的例外模組。"""
    offenders: list[str] = []
    for path in _product_python_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in _PROCESS_LAUNCH_ALLOWED:
            continue
        offenders.extend(
            f"{relative}:{lineno} {what}" for lineno, what in _process_launches(_parse(path))
        )
    assert offenders == [], (
        "發現未經 services/execution_broker.py 的子程序啟動（ADR-0239）："
        f"{offenders}"
    )


def test_broker_is_the_launcher() -> None:
    launches = {what for _, what in _process_launches(_parse(BROKER))}
    assert {"subprocess.run", "asyncio.create_subprocess_exec"} <= launches


# ── iso-c2：exec／fork 類呼叫只允許隔離啟動器 ──────────────────────────────────────
# 啟動器（信任根）在 Landlock 限制後 exec 指令、自測時 fork；其他產品模組一律經 broker。
SANDBOX_LAUNCHER = ROOT / "ToukaProxy" / "src" / "sandbox_kernel" / "launcher.py"
_EXEC_FORK_ALLOWED = frozenset({SANDBOX_LAUNCHER.relative_to(ROOT).as_posix()})
_OS_EXEC_FORK = re.compile(r"exec[lv]p?e?|fork|forkpty|posix_spawnp?|spawn[lv]p?e?")
_PTY_EXEC_FORK = frozenset({"spawn", "fork"})


def _exec_fork_calls(tree: ast.Module) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"os", "pty"}:
            names = [alias.name for alias in node.names]
            found.extend(
                (node.lineno, f"from {node.module} import {name}")
                for name in names
                if name == "*"
                or (node.module == "os" and _OS_EXEC_FORK.fullmatch(name))
                or (node.module == "pty" and name in _PTY_EXEC_FORK)
            )
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner, attr = node.value.id, node.attr
            if (owner == "os" and _OS_EXEC_FORK.fullmatch(attr)) or (
                owner == "pty" and attr in _PTY_EXEC_FORK
            ):
                found.append((node.lineno, f"{owner}.{attr}"))
    return found


def test_exec_and_fork_only_in_sandbox_launcher() -> None:
    """os.exec*／fork／posix_spawn／spawn*、pty.spawn／fork 只能出現在隔離啟動器（含引用）。"""
    offenders: list[str] = []
    for path in _product_python_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in _EXEC_FORK_ALLOWED:
            continue
        offenders.extend(
            f"{relative}:{lineno} {what}" for lineno, what in _exec_fork_calls(_parse(path))
        )
    assert offenders == [], f"exec／fork 類呼叫只允許隔離啟動器：{offenders}"


def test_sandbox_launcher_is_the_only_exec_site() -> None:
    """正向對照：掃描真的認得啟動器裡的 exec 與 fork（掃描壞掉時這裡會紅）。"""
    calls = {what for _, what in _exec_fork_calls(_parse(SANDBOX_LAUNCHER))}
    assert {"os.execvpe", "os.execv", "os.fork"} <= calls
    assert not _process_launches(_parse(SANDBOX_LAUNCHER))


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.execve('x', [], {})\n",
        "import os\nos.posix_spawn('x', [], {})\n",
        "import os\nos.spawnlp(0, 'x', 'x')\n",
        "import os\npid = os.fork()\n",
        "import os\nlaunch = os.execv\n",
        "from os import execvp\n",
        "from os import *\n",
        "import pty\npty.spawn(['x'])\n",
    ],
)
def test_exec_fork_scan_catches_every_form(source: str) -> None:
    assert _exec_fork_calls(ast.parse(source))
