"""官方 MCP 伺服器啟動規格對照表（SSOT）＋查表純函式。

修 F1/F2（ADR-0186）：``MCPHubService.fetch_official_servers`` 原把官方 repo 每個目錄
一律映射成 ``npx -y @modelcontextprotocol/server-{name}``，但 fetch/git/time 實為 PyPI
套件（須 ``uvx mcp-server-*``，npm 同名 404）、sequentialthinking 的 npm 正名帶連字號
（``server-sequential-thinking``）、filesystem 未帶允許目錄——client 未宣告 roots 協定
時官方 server 初始化即拋錯。七條套件名皆經 npm/PyPI registry 逐條實證（2026-07-24）。

本模組是官方條目「怎麼啟動」的唯一事實來源：

- 已知目錄 → 查表回傳實證過的 ``(command, args)``；filesystem 附 ``@TOUKA_APPDATA@``
  哨兵作允許目錄（install／connect 雙端解析，解析 SSOT＝``mcp_sentinels``）。
- 未知目錄 → fallback npx 慣例（新官方 server 歷來以 npm 為主）、``known=False``，
  由呼叫端記 WARNING（無聲變有聲）。

設計：純資料＋純函式、零 IO/時間/環境依賴（體例比照 ``mcp_category``）。
"""
from __future__ import annotations

from src.utils.mcp_category import classify_mcp_category
from src.utils.mcp_sentinels import APPDATA_SENTINEL

# 官方 repo 目錄名 → (command, args)。tuple 保 SSOT 常量不可變；查表函式回傳新 list。
#
# **釘版政策（ADR-0191）**：套件名一律釘定版本——npm 走 ``pkg@x.y.z``、PyPI 走 PEP 440
# 的 ``pkg==x.y.z``（`uvx` 實測支援）。未釘版＝安裝當下抓上游最新版，等同把供應鏈信任
# 交給「未來某個尚未存在的版本」（禁令 55「只裝人工已審、pinned 來源」的精神延伸）。
# 副作用（刻意）：釘版讓前端 ``computeMcpStatus`` 的「可更新」徽章第一次有真實意義
# ——策展人升版本＝使用者看得到可更新，而非過去的恆無訊號。
#
# 升級 SOP：`npm view <pkg> version` ／ `https://pypi.org/pypi/<pkg>/json` 查新版 →
# 改本表 → 種子 parity 測試會紅（種子＝配方快照）→ 同步更新 ``mcp_hub.json.example``。
# 版本經 2026-07-25 registry 實查。
OFFICIAL_LAUNCH_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "everything": ("npx", ("-y", "@modelcontextprotocol/server-everything@2026.7.4")),
    "fetch": ("uvx", ("mcp-server-fetch==2026.7.10",)),
    "filesystem": (
        "npx",
        ("-y", "@modelcontextprotocol/server-filesystem@2026.7.10", APPDATA_SENTINEL),
    ),
    "git": ("uvx", ("mcp-server-git==2026.7.10",)),
    "memory": ("npx", ("-y", "@modelcontextprotocol/server-memory@2026.7.4")),
    "sequentialthinking": (
        "npx",
        ("-y", "@modelcontextprotocol/server-sequential-thinking@2026.7.4"),
    ),
    "time": ("uvx", ("mcp-server-time==2026.7.10",)),
}


def official_launch_spec(dir_name: str) -> tuple[str, list[str], bool]:
    """回傳官方目錄的啟動規格 ``(command, args, known)``（純函式）。

    未知目錄 fallback npx 慣例（``known=False``，套件未實證）；args 恆回新 list，
    呼叫端可安全變異。**fallback 條目刻意不釘版**——版本未知就無從釘起，誠實維持浮動
    並由呼叫端記 WARNING；種子只收 ``known=True`` 條目，故釘版棘輪不受 fallback 影響。
    """
    spec = OFFICIAL_LAUNCH_SPECS.get(dir_name)
    if spec:
        return spec[0], list(spec[1]), True
    return "npx", ["-y", f"@modelcontextprotocol/server-{dir_name}"], False


def official_hub_entry(dir_name: str) -> tuple[dict, bool]:
    """官方目錄 → 完整 hub entry dict（啟動規格＋展示欄位）＋``known`` 旗標（配方 SSOT）。

    ``fetch_official_servers``（sync）與 ``mcp_hub.json.example`` 出廠種子的 parity
    測試共用同一配方——種子是本配方的快照，逐欄相等由 seed-integrity 測試釘死
    （ADR-0189；啟動規格若隨 ADR-0186 表演進，種子漂移立刻紅）。
    """
    command, args, known = official_launch_spec(dir_name)
    entry = {
        "id": f"official-{dir_name}",
        "name": dir_name.replace("-", " ").title(),
        "category": classify_mcp_category(dir_name),
        "description": f"Anthropic 官方維護的 {dir_name} 伺服器。",
        "command": command,
        "args": args,
        "env_requirements": [],
        "source": "official",
        "is_official": True,
        "popularity": 999999,
        "icon": "ShieldCheck",
    }
    return entry, known
