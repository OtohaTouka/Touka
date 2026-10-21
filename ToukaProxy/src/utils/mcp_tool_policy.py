"""MCP 工具面治理 SSOT（ADR-0195）——兩軸並存的純比對函式。

Touka 在此之前只有 **per-server** 粒度：`permission_service.is_mcp_server_allowed` 決定
「掛不掛這台」，掛上去就是整台的工具全開。本模組是把粒度降到**單一工具**的唯一比對來源。

兩軸語意刻意不同，**不可互相取代**：

**軸 A｜per-installation（供給面）**
    `enabled_tools`／`disabled_tools` 寫在伺服器條目（``ProtocolServerConfig``）上，語意是
    「這台伺服器我這次安裝打算開放哪些工具」。它**不是權限邊界**，是 owner 對自己 agent 的
    自我約束（等同 Square MCP 的 ``DISALLOW_WRITES``、M365 MCP 的 ``--read-only``），因此
    **所有身分一律適用、Owner 也不例外**。

    ⚠️ 這正是它**不能**走 `permission_service` 的原因：`get_allowed_tools` 對 Owner 短路回
    ``["*"]``、`is_mcp_server_allowed` 對 Owner 恆回 True——軸 A 一旦接進去就被 Owner 繞過，
    而 Owner ＋ 含通用寫入原語的伺服器（如 Stripe 的 ``stripe_api_write``／``create_refund``）
    正是這條治理線唯一的實際受害者。故軸 A 的執法點在**供給面**（``ProtocolManager``）。

**軸 B｜per-role（身分面）**
    ``server/tool`` 形態的條目寫在角色（``RoleConfig``）上，是真正的權限邊界，執法點在
    `permission_service`。Owner 仍於 allow 面短路（角色模型不變，ADR-0141），但 deny 面
    對 Owner 同樣生效——與既有 `denied_tools` 在 `is_authorized` 的語意一致。

兩軸取**交集**：任一軸拒絕即拒絕，沒有任何一軸能授予另一軸拒絕的東西。

工具名稱一律為**裸名**（MCP 伺服器自己回報的 ``tool.name``，如 ``create_refund``），不是
廣告面的 ``server__tool`` 前綴形式——前綴是 `get_all_tools` 組裝出來的顯示名，設定檔與 UI
勾選面看到的都是裸名。

本模組純函式：不讀檔、不碰身分、無 IO、無時間依賴。
"""

from __future__ import annotations

from typing import Any

WILDCARD = "*"
"""全域萬用符：在 ``enabled_tools`` 代表全開；在軸 B 的 allow 面代表全部伺服器。"""

POLICY_FIELDS: tuple[str, ...] = (
    "enabled_tools",
    "disabled_tools",
    "vault_jail_arg",
    "protocol_mode",
    "auth_mode",
    "auth_profile_id",
    "workload_authority_kind",
    "workload_delegation_id",
    "workload_source_installation_id",
    "capability_policy",
)
"""伺服器條目上的「政策欄位」：任何**整條目重寫**的寫入路徑都必須把它們帶過去。

除既有工具／vault 政策外，協定模式、認證輪廓與能力政策亦不得因舊前端或例行重註冊
而退回預設值。

來源（``origin``）與額外允許的資料夾（``sandbox_paths``）刻意不在這裡：它們不是
``server_config.json`` 的欄位，存在 MCP 隔離設定檔、以 ``bound`` 綁條目的程式身分，由每個
寫入點依程式路徑自己寫（ADR-0275）。帶過去會讓演化覆寫同名的 Hub 條目時沿用
``hub``，或把人給第三方的資料夾套到代理人的程式上。
"""


def carry_over_policy(
    new_entry: dict[str, Any], old_entry: dict[str, Any] | None
) -> dict[str, Any]:
    """整條目重寫時保住政策欄位，回傳**新的 dict**（不改寫任何輸入）。

    背景：`register_server`／技能市集安裝／自我演化建立三條路徑都以字面量重建整個 entry
    （只帶 command/args/env/enabled），同名重註冊即把該台的隔離與工具治理政策一起抹掉。
    這是 vault_jail_arg 當初那條「政策因例行操作 fail open」複審缺陷的同型再現。

    ⚠️ **刻意回傳副本而非就地改寫**：呼叫端同時持有「原始 mcp_config」與「帶回政策後的
    entry」兩個變數，若兩者是同一個物件，用錯變數（例如熱掛載傳 ``mcp_config``）今天會
    因別名而僥倖正確、日後任一處改成建副本就無聲斷裂，且測試殺不掉。純函式讓「用錯變數」
    立刻是可觀測的行為差異。

    僅在 ``new_entry`` **未提供**該欄位時回填——呼叫端明示設值（含 ``[]`` 明示全關）一律優先。
    """
    merged = dict(new_entry)
    if not old_entry:
        return merged
    for field in POLICY_FIELDS:
        if field not in merged and old_entry.get(field) is not None:
            merged[field] = old_entry[field]
    return merged


def _matches_name(tool_name: str, patterns: list[str]) -> bool:
    """裸工具名是否命中清單（精確比對，``*`` 為全命中）。

    刻意**不支援** ``create_*`` 這類前綴 glob：勾選面是一份明確的工具清單，模糊比對只會讓
    「我到底開了什麼」變得不可讀，也讓上游新增工具時靜默落進既有樣式。
    """
    return WILDCARD in patterns or tool_name in patterns


def is_tool_enabled(
    tool_name: str,
    enabled_tools: list[str] | None = None,
    disabled_tools: list[str] | None = None,
) -> bool:
    """軸 A：這台伺服器的這個工具是否被此次安裝開放。

    Args:
        tool_name: 裸工具名（MCP 伺服器回報的 ``tool.name``）。
        enabled_tools: 白名單。``None`` ＝**未設定＝全開**（升級既有條目時不會整批消失）；
            ``[]`` ＝**明示全關**（使用者在 UI 把全部取消勾選）。此二者語意不同，不可合併。
        disabled_tools: 黑名單，**恆優先於白名單**（deny wins）。

    Returns:
        bool: 是否開放。
    """
    if disabled_tools and _matches_name(tool_name, disabled_tools):
        return False
    if enabled_tools is None:
        return True
    return _matches_name(tool_name, enabled_tools)


def server_of_ref(pattern: str) -> str | None:
    """軸 B：取出條目的伺服器部位；全域萬用符 ``*`` 回 ``None``（不指向特定伺服器）。

    ``"stripe"`` → ``"stripe"``；``"stripe/create_refund"`` → ``"stripe"``；``"*"`` → ``None``。
    """
    if pattern == WILDCARD:
        return None
    return pattern.split("/", 1)[0]


def validate_tool_ref(ref: object, *, allow_bare_wildcard: bool) -> str | None:
    """驗證一條 ``server/tool`` 條目的**字串形狀**；合法回 ``None``，否則回中文原因。

    合法形態（與 `matches_tool_ref` 的比對規則一一對應，兩者必須同進同退）：
    ``"*"``（是否接受由呼叫端決定）、``"server"``、``"server/*"``、``"server/tool"``。

    ⚠️ **刻意只在 CI 守衛（Guard #32）執法、runtime 不驗形狀**：P5 已讓
    `permissions.json` 的 schema／JSON 解析失敗在啟動期 fail-fast、熱重載期保留
    last-known-good，不再降級至 fallback；但工具參照形狀仍屬部署期完整性檢查，
    不把會隨 registry 變動的 CI 規則升格為 runtime 可用性依賴。形狀漂移由 Guard #32
    fail-loud 阻擋，runtime 則只負責既有 Pydantic 合約與安全的載入生命週期。

    誠實邊界：本函式**驗不了「工具真的存在」**——MCP 工具由 hub 動態提供、隨伺服器版本
    變動，無法靜態窮舉（同 Guard #32 對 `allowed_mcp_servers` 既有的註記）。它只擋
    「形狀就不可能匹配」的死字串。

    Args:
        ref: 待驗條目（非字串亦視為違規，設定檔可能寫成數字或 null）。
        allow_bare_wildcard: 是否接受裸 ``"*"``。P2 後 allow 面代表開放式授權，
            deny 面代表該角色或全域地板的 deny-all。

    Returns:
        str | None: 違規原因；合法時為 ``None``。
    """
    if not isinstance(ref, str):
        return "非字串"
    if ref != ref.strip():
        return "前後有空白"
    if not ref:
        return "空字串"
    if ref == WILDCARD:
        return None if allow_bare_wildcard else "deny 面不接受裸 '*'（請用 'server/*'）"
    parts = ref.split("/")
    if len(parts) > 2:
        return "斜線過多（僅支援 'server' 或 'server/tool'）"
    if any(not p for p in parts):
        return "伺服器或工具部位為空"
    if any(any(c.isspace() for c in p) for p in parts):
        return "含空白字元"
    if len(parts) == 2 and parts[0] == WILDCARD:
        return "伺服器部位不得為 '*'（萬用只支援工具部位）"
    return None


def matches_tool_ref(pattern: str, server_name: str, tool_name: str) -> bool:
    """軸 B：``server/tool`` 條目是否命中這個 (伺服器, 工具)。

    支援形態：
    - ``"*"``：全部伺服器的全部工具。
    - ``"stripe"``：該台**整台**（所有工具）。
    - ``"stripe/*"``：同上，明示形。
    - ``"stripe/create_refund"``：該台的單一工具。

    Args:
        pattern: 條目樣式。
        server_name: MCP 伺服器鍵（``derive_server_key`` 產出的鍵，與 registry 同鍵）。
        tool_name: 裸工具名。

    Returns:
        bool: 是否命中。
    """
    if pattern == WILDCARD:
        return True
    if "/" not in pattern:
        return pattern == server_name
    ref_server, ref_tool = pattern.split("/", 1)
    if ref_server != server_name:
        return False
    return ref_tool in (WILDCARD, tool_name)
