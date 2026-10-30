"""隔離層級 SSOT 的純函式與常數（iso-c2）.

代理人撰寫或指定的程式依 ``executor``／``landlock``／``none`` 執行；層級只由
``services/isolation`` 判定。這裡只放設定檔改不鬆的程式常數與純函式（禁令 18）。
"""

from __future__ import annotations

import re
from typing import Literal, cast, get_args

# Landlock ABI 對照（SSOT 在只用標準函式庫的 kernel）：未驗證的權利不用；landlock 層要擋 TCP
# （ABI 4），executor 層的網路靠容器分離，只要檔案牆（ABI 3）。
from src.sandbox_kernel.landlock import MIN_ABI as EXECUTOR_LAYER_MIN_ABI
from src.sandbox_kernel.landlock import MIN_NET_ABI as LANDLOCK_LAYER_MIN_ABI
from src.sandbox_kernel.landlock import VERIFIED_ABI_MAX as VERIFIED_ABI_MAX
from src.sandbox_kernel.landlock import effective_abi
from src.sandbox_kernel.spec import PUBLIC_CONFIG_NAMES, fold_name, listing_kind

IsolationLevel = Literal["none", "landlock", "executor"]
ISOLATION_LEVELS: tuple[str, ...] = get_args(IsolationLevel)

TERMINAL_ACTION = "terminal_execute"
# 終端以外、一定要在隔離下執行的工具（含兩個演化舊別名）
ISOLATION_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {
        "evolution_implement_and_verify",
        "write_mcp_implementation",
        "verify_mcp_locally",
        "create_mcp",
        "scaffold_mcp",
    }
)
# 終端工具的兩個註冊名（同 BUILTIN_EXECUTION_BINDINGS）與互審驗證指令（同意帳本的種類名）
TERMINAL_TOOLS: frozenset[str] = frozenset({"execute_command", "bash"})
VERIFY_ACTION = "verify"
# 這個版本「真的經過 runner」的動作。要隔離、但不在這裡的動作，
# 政策層級一律 none（RUNNER_NOT_WIRED）：Auto／YOLO 要人核准，無人在場拒絕，卡片照實寫「未隔離」。
# 新增動作前必須先在同一個 PR 把它接上 runner，不能只改這份清單。
ISOLATION_WIRED_ACTIONS: frozenset[str] = TERMINAL_TOOLS | {
    VERIFY_ACTION,
    "evolution_implement_and_verify",
    "write_mcp_implementation",
    "verify_mcp_locally",
    "create_mcp",
    "scaffold_mcp",
}
RUNNER_NOT_WIRED = "runner_not_wired"
# 要隔離、但沒接 runner 的動作：審批卡依工具一律寫「未隔離」（direct），不看帳本與原因
ISOLATION_UNWIRED_TOOLS: frozenset[str] = ISOLATION_REQUIRED_TOOLS - ISOLATION_WIRED_ACTIONS

# MCP 伺服器的來源（C2-3）：只由各寫入點依程式路徑寫入，模型參數、前端與匯入都決定不了。
# agent＝人在設定頁標成「代理人建立」（只能往嚴）。紀錄裡看不懂的值當自建（不能當成較寬的類別）。
McpOrigin = Literal["hub", "skill", "manual", "creation", "scaffold", "evolution", "agent"]
MCP_ORIGINS: tuple[str, ...] = get_args(McpOrigin)
MCP_SELF_ORIGINS: frozenset[str] = frozenset({"creation", "evolution", "agent"})
MCP_THIRD_PARTY_ORIGINS: frozenset[str] = frozenset({"hub", "skill", "manual"})
MCP_SCAFFOLD_ORIGIN = "scaffold"
# 分類結果：自建、Touka 產生的 scaffold、第三方；有 url 的是遠端（不是子行程，不包）
McpClass = Literal["self", "scaffold", "third_party", "remote"]
# 人設的「額外允許的資料夾」上限（存檔與啟動都過永不授權）
MAX_MCP_SANDBOX_PATHS = 16

# 每個 feature 最低需要的 ABI（fs 3、tcp 4、ioctl 5、信號與抽象 socket 6）
TCP_FEATURE = "tcp"
_FEATURE_MIN_ABI: dict[str, int] = {
    "fs": EXECUTOR_LAYER_MIN_ABI,
    TCP_FEATURE: LANDLOCK_LAYER_MIN_ABI,
    "ioctl": 5,
    "signal": 6,
    "abstract_unix": 6,
}

# Touka 目錄底下不能授權的頂層名稱（NFC＋casefold）：repo 已追蹤的頂層名稱、`.gitignore`
# 的頂層名稱與 `scratch`，由 test_toplevel_protected_sync 對 `git ls-files`／`.gitignore` 同步。
# 所有點開頭的名稱另由規則涵蓋，不列在這裡。
TOUKA_TOPLEVEL_PROTECTED: frozenset[str] = frozenset(
    {
        "toukachat", "toukadispatch", "toukaheartbeat", "toukaide", "toukamemory", "toukaproxy",
        "docs", "nginx", "scripts", "searxng", "tests", "violations",
        "agents.md", "contributing.md", "handover.md", "license", "notice", "readme.md",
        "readme_quickstart.md", "security.md", "openapi.json", "mkdocs.yml",
        "rebuildable-outputs.txt", "pyproject.toml", "pytest.ini", "ruff.toml", "uv.lock",
        "docker-compose.yml", "docker-compose.cloud.yml", "docker-compose.ghcr.yml",
        "docker-compose.searxng.yml", "docker-compose.override.yml",
        "data", "backups", "heartbeat-config", "heartbeat-config.json", "venv", "env",
        "node_modules", "__pycache__", "site", "thumbs.db", "handover_heartbeat.md", "sns.md",
        "溝通協調.md", "repomix-output.xml", "touka-docs-repomix.xml", "touka-code-repomix.xml",
        "scratch",
    }
)  # fmt: skip


def normalize_isolation(value: object) -> IsolationLevel:
    """看不懂的值或缺鍵一律當 ``none``（fail-closed）。"""
    if isinstance(value, str) and value in ISOLATION_LEVELS:
        return cast("IsolationLevel", value)
    return "none"


def level_rank(level: IsolationLevel) -> int:
    """層級強弱：none < landlock < executor。"""
    return ISOLATION_LEVELS.index(level)


def requires_isolation(action: str, resource: str) -> bool:
    """終端指令，或資源名稱在隔離必要清單裡。"""
    return action == TERMINAL_ACTION or resource in ISOLATION_REQUIRED_TOOLS


def is_public_config_name(name: str) -> bool:
    """名稱比對不分大小寫（9p 與 NTFS 不分大小寫）；清單 SSOT 是 ``sandbox_kernel.spec``。"""
    return name.casefold() in PUBLIC_CONFIG_NAMES


def landlock_features(abi: int) -> frozenset[str]:
    """依 ABI 對照表回傳實際生效的權利。"""
    abi = effective_abi(abi)
    return frozenset(name for name, min_abi in _FEATURE_MIN_ABI.items() if abi >= min_abi)


# 技能名稱（演化工具的資料夾 ``mcp_servers/managed/<skill>``）：單層、英數開頭
SKILL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
WINDOWS_RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul"}) | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def is_skill_segment(name: object) -> bool:
    """技能名稱的唯一判斷（ADR-0293 P9）：``validate_skill_id`` 與 ``tool_root`` 都只經這裡.

    技能名稱會成為執行器可寫掛載根 ``managed`` 的第一層；執行器把掛載根第一層是 Touka 目錄
    名稱（kernel 的 ``DATA_ROOT_NAMES``、``TOUKA_MARKERS``，``fold_name`` 後比對）判成
    ``touka_root``、不服務，所以這些名字也不能當技能名稱。
    """
    return (
        isinstance(name, str)
        and SKILL_ID_PATTERN.fullmatch(name) is not None
        and name.casefold() not in WINDOWS_RESERVED_NAMES
        and listing_kind({fold_name(name)}) is None
    )


def is_safe_segment(name: str) -> bool:
    """識別碼與名稱必須是單一安全路徑段（拒 ``..``、分隔符、NUL、空白）。"""
    stripped = (name or "").strip()
    return bool(stripped) and stripped == name and stripped not in {".", ".."} and not any(
        ch in name for ch in ("/", "\\", "\x00")
    )
