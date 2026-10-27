# ruff: noqa: E402, T201, BLE001
"""Smoke Test: Native Builtin Tool Coverage Sweep (Track A1).

系統化驗證「全部原生工具」皆已註冊、結構完整且可安全調用——把人工在 Debug Center
逐一試工具的流程自動化為可重複測試。

策略（安全第一，鏡像 /v1/debug/execute 的執行路徑）：
- 完整性：tool_registry 每個工具皆 callable，且在 builtin_tools_schema 有對應 schema。
- 真實調用（安全子集）：唯讀／沙箱／空參數安全早退的工具，經 ToolExecutor 實際執行，
  斷言回傳結構化字串、發出 tool_result 事件、無未處理例外。
- 僅結構檢查（破壞性／外部／阻塞工具，不實際執行）：create_*/scaffold/evolution/sns/
  connect_mcp/activate_skill/request_skill_install/ask_user/research/ide_open_diff
  —— 僅驗證已註冊＋可呼叫＋（適用時）有 schema，避免系統級寫入、外部送出或阻塞。

身分：DebugAgent（auth_level=Manual）。真實調用僅作用於 DebugAgent 自身範圍，
不污染其他 agent 資料。
"""

import asyncio
import sys
from pathlib import Path

from lib.sandbox_env import enable_smoke_sandbox, run_cleanup_steps

enable_smoke_sandbox("tool_coverage")

# 1. 路徑 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"
for _p in (PROXY_DIR, MEMORY_SRC_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from memory_server.factual import FactualStore
from memory_server.tools import memory_tool_registry, memory_tools_schema
from memory_server.vector import VectorStore
from src.models.identity import AgentIdentity
from src.services.tool_executor import ToolExecutor
from src.tools.builtin import builtin_tools_schema, tool_registry

# 真實調用的安全工具 → 參數（唯讀／沙箱／空參數安全早退；無系統級或外部副作用）
SAFE_INVOKE: dict[str, dict] = {
    "vault_tools": {"vault": "personal", "action": "list"},
    "memory_tools": {"action": "query", "query": "smoke coverage probe"},
    "mission_tools": {"action": "get_board"},
    "agent_control": {"action": "get_status"},
    "search_skill_market": {},
    "bash": {"command": "echo touka_smoke_ok"},
    "execute_command": {"command": "echo touka_smoke_ok"},
    "get_command_result": {},  # CRITICAL_TOOLS 空參數 → 安全早退
    "update_my_soul": {},  # CRITICAL_TOOLS 空參數 → 安全早退（不改 soul）
    "project_tools": {},  # CRITICAL_TOOLS 空參數 → 安全早退
    # Tier 3 統一動詞（唯讀子集＋空參早退）
    "list": {"scope": "personal"},
    "read": {},  # CRITICAL_TOOLS 空參數 → 安全早退
    "edit_soul": {},  # CRITICAL_TOOLS 空參數 → 安全早退（不改 soul）
}

# 僅結構檢查、不實際執行（破壞性／外部／阻塞）
WIRED_ONLY: dict[str, str] = {
    "ask_user": "阻塞等待人類回答",
    "sns_control": "外部發送（Discord/Telegram）",
    "send_message": "外部發送（Discord/Telegram；sns_control 正名）",
    "stop_command": "會終止真實指令執行狀態",
    "dispatch_subagent": "會啟動模型子迴圈並消耗 API 額度",
    "dispatch_subagent_group": "會啟動多個模型子迴圈並消耗 API 額度",
    "write": "寫入動詞（smoke 不做實寫）",
    "edit": "寫入動詞（smoke 不做實寫）",
    "insert": "寫入動詞（smoke 不做實寫）",
    "undo": "寫入動詞（smoke 不做實寫）",
    "delete": "寫入動詞（smoke 不做實寫）",
    "find": "唯讀（涵蓋於 list/read 已足）",
    "grep": "唯讀（涵蓋於 list/read 已足）",
    "connect_mcp_server": "會實際連線 MCP 伺服器",
    "activate_skill": "變更 session 技能狀態",
    "request_skill_install": "建立技能安裝請求",
    "create_skill": "creation engine 系統級寫入",
    "create_mcp": "creation engine 系統級寫入",
    "scaffold_mcp": "creation engine 系統級寫入",
    "propose_new_capability": "演化（系統級）",
    "write_mcp_implementation": "演化（產生程式碼）",
    "verify_mcp_locally": "演化（執行子程序）",
    "request_mcp_creation": "演化（系統級）",
    "evolution_define": "演化（系統級寫入）",
    "evolution_implement_and_verify": "演化（產生程式碼＋執行子程序）",
    "ide_open_diff": "需 IDE bridge；非核心系統功能",
    "research": "真實網路搜尋（耗費 API credit）",
}

# schema 由 memory_tools_schema 提供或動態注入（dispatcher-gated），不在 builtin_tools_schema。
# Tier 3 雙軌：舊 god-tools 降為 shim（可執行、不再廣告）→ 一併豁免（LEGACY_SHIM_SCHEMAS
# 保有其形狀文件；廣告面由統一動詞＋edit_soul＋send_message 接手）。
SCHEMA_EXEMPT = {
    "connect_mcp_server",
    "activate_skill",
    "search_skill_market",
    "project_tools",  # Tier 3 shim（→ 統一動詞 scope=project）
    "vault_tools",  # Tier 3 shim（→ 統一動詞 scope=personal/legacy/soul）
    "update_my_soul",  # Tier 3 shim（寫→edit_soul；讀→read(scope=soul)）
    "sns_control",  # Tier 3 shim（→ send_message）
    "bash",  # Tier 3 shim（→ execute_command 非同步審批）
    "propose_new_capability",  # Tier 3 shim（→ evolution_define）
    "write_mcp_implementation",  # Tier 3 shim（→ evolution_implement_and_verify）
    "verify_mcp_locally",  # Tier 3 shim（→ evolution_implement_and_verify）
    "request_mcp_creation",  # Tier 3 shim（→ …(submit_for_deploy=true)）
}

# 工具結果若含這些標記，代表 Python 層級例外或工具路由領域錯誤，皆不是成功調用。
INTERNAL_ERR_MARKERS = (
    "unexpected keyword argument",
    "Traceback (most recent call last)",
    "got multiple values for",
    "positional argument",
)
DOMAIN_ERR_MARKERS = (
    "Unknown action",
    "Unknown tool",
)
FAILURE_MARKERS = INTERNAL_ERR_MARKERS + DOMAIN_ERR_MARKERS


def _schema_names(schemas) -> set[str]:
    """從 OpenAI function schema 清單擷取工具名（容忍 {function:{name}} 與 {name} 兩種形狀）。"""
    names: set[str] = set()
    for s in schemas:
        if not isinstance(s, dict):
            continue
        fn = s.get("function") if isinstance(s.get("function"), dict) else s
        name = fn.get("name") or s.get("name")
        if name:
            names.add(name)
    return names


async def _invoke(executor: ToolExecutor, name: str, args: dict, identity: AgentIdentity):
    """以 DebugAgent 經執行器跑一個工具，回傳 (結果字串, 事件型別清單)。"""
    q: asyncio.Queue = asyncio.Queue()
    result = await executor.execute_to_queue(
        name=name,
        args=args,
        request_id="smoke_a1",
        ctx={
            "agent_id": identity.agent_id,
            "session_id": None,
            "identity": identity,
            # 冒煙的目的是「工具真的執行、回領域結果」——H1 後 Manual 對非唯讀工具
            # fail-closed（permission_denied），故以 YOLO 跑覆蓋掃描。
            "auth_level": "YOLO",
            "model": "DebugAgent",
        },
        queue=q,
    )
    events = []
    while not q.empty():
        ev = q.get_nowait()
        if ev is not None:
            events.append(ev.get("type"))
    return result, events


async def main() -> None:
    print("[*] Track A1 — Native Builtin Tool Coverage Sweep")
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", capabilities=["*"])
    executor = ToolExecutor(
        protocol_manager=None,
        tool_registry=tool_registry,
        memory_tool_registry=memory_tool_registry,
        plugin_registry={},
        enforce_advertised=False,
    )

    failures: list[str] = []
    combined_registry = {**memory_tool_registry, **tool_registry}
    reg_names = set(combined_registry)
    schema_names = _schema_names([*builtin_tools_schema, *memory_tools_schema])

    # --- Part 1: 完整性（每工具可呼叫；每工具有 schema） ---
    print("\n[1] Registry / Schema integrity ...")
    for name, fn in combined_registry.items():
        if not callable(fn):
            failures.append(f"{name}: 不可呼叫 (not callable)")
    hard_missing = (reg_names - schema_names) - SCHEMA_EXEMPT
    if hard_missing:
        failures.append(f"無對應 schema 的工具: {sorted(hard_missing)}")
    print(
        f"    {len(reg_names)} 工具註冊 / {len(schema_names)} schema; "
        f"缺 schema(非動態)={sorted(hard_missing)}"
    )

    # --- Part 2: 真實調用安全子集 ---
    print("\n[2] 真實調用安全子集 ...")
    for name, args in SAFE_INVOKE.items():
        if name not in combined_registry:
            failures.append(f"{name}: 不在 registry（SAFE_INVOKE 清單過期）")
            continue
        try:
            result, events = await _invoke(executor, name, args, identity)
            preview = str(result).replace("\n", " ")[:80]
            failed_marker = next(
                (m for m in FAILURE_MARKERS if m.casefold() in str(result).casefold()),
                None,
            )
            if not isinstance(result, str):
                failures.append(f"{name}: 回傳非字串 ({type(result).__name__})")
            if "tool_result" not in events:
                failures.append(f"{name}: 未發出 tool_result 事件 (events={events})")
            if failed_marker:
                failures.append(
                    f"{name}: 工具結果為內部/領域錯誤（標記 '{failed_marker}'）-> {preview!r}"
                )
            tag = "FAIL" if failed_marker else "OK"
            print(f"    [{tag}] {name}: events={events} -> {preview!r}")
        except Exception as e:
            failures.append(f"{name}: 未處理例外 {type(e).__name__}: {e}")
            print(f"    [FAIL] {name}: {type(e).__name__}: {e}")

    # --- Part 3: 僅結構檢查（不執行） ---
    print("\n[3] 僅結構檢查（破壞性/外部/阻塞，不執行） ...")
    for name, reason in WIRED_ONLY.items():
        if name not in combined_registry or not callable(combined_registry[name]):
            failures.append(f"{name}: 列為 wired-only 但未註冊/不可呼叫")
        else:
            print(f"    [WIRED] {name} — {reason}")

    # --- 覆蓋率：每個註冊工具都須被分類（SAFE_INVOKE ∪ WIRED_ONLY） ---
    uncovered = reg_names - set(SAFE_INVOKE) - set(WIRED_ONLY)
    if uncovered:
        failures.append(f"未分類工具（請補進掃描表）: {sorted(uncovered)}")

    print("\n" + "=" * 56)
    if failures:
        print(f"[FAIL] 發現 {len(failures)} 個問題：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(
        f"[SUCCESS] 全部 {len(reg_names)} 原生工具覆蓋："
        f"{len(SAFE_INVOKE)} 實跑 / {len(WIRED_ONLY)} 結構檢查。"
    )
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform == "win32":
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    try:
        asyncio.run(main())
    finally:
        run_cleanup_steps(
            VectorStore.close_all_clients,
            FactualStore.close_all_connections,
        )
