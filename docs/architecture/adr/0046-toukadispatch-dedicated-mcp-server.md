# ADR-0046: ToukaDispatch — 專用 MCP 派發伺服器與安全加固 (C1/C2/C3/H1/H2)

**狀態**：✅ 已接受  
**日期**：2026-05-26  
**影響範圍**：ToukaDispatch + ToukaMemory  
**關鍵字**：ToukaDispatch, FastMCP, DISPATCH_TOKEN, identity_resolver, C1, H1, H2, 安全加固

> 🗑️ **兩處已被後續 ADR 取代（§3 FastMCP 自癒段已有註，此處補 §2 與決策 #1）**：①**§2 DISPATCH_TOKEN 單段 Bearer 模型／`_check_token` 碼例**已被 [ADR-0047](0047-dispatch-session-token-handshake.md)／[ADR-0050](0050-dispatch-handshake-reinstate-token-param.md) 的 handshake→session_token 流取代（函式改名 `_verify_dispatch_token`、三操作工具只收 `session_token`）；②**決策 #1「`ToukaMemory/server.py` 保持純記憶服務」**——該檔已被 [ADR-0116](0116-toukamemory-integration-only.md) 整個刪除（獨立 MCP 模式退役）。C1（無 FORCE_AGENT_ID）/H1（BLOCKED 需 blocker_detail）/H2（path-traversal field_validator）核心安全決策仍有效。

---

## 背景 (Context)

ADR-0044 引入了 AI Agent 工作分派系統，將 `ToukaMemory/server.py` 同時作為記憶 MCP 伺服器與任務派發入口，並實作了「憑證缺失時自動注入 `can_supervise_dispatch` 能力」的自癒降級機制。

審計後發現此機制帶來以下安全問題：

| 編號 | 等級 | 描述 |
|------|------|------|
| C1 | Critical | `FORCE_AGENT_ID` 環境變數允許任意外部請求假冒任何 Agent 身份 |
| C2 | Critical | `FallbackIdentity` 行內類別重複定義，權限檢查邏輯分散 |
| C3 | Critical | `role="base"` (小寫) 與系統 RBAC 不一致，可能繞過角色判定 |
| H1 | High | 任務轉換為 `BLOCKED` 不要求填寫阻礙原因，導致無效狀態積累 |
| H2 | High | `Task` Pydantic 模型未啟用 `validate_assignment=True`，直接屬性賦值可繞過路徑穿越防禦 |

---

## 決策 (Decision)

### 1. 建立 ToukaDispatch 專用 FastMCP 伺服器（Port 8083）

- 新增子模組 `ToukaDispatch/`，包含 `server.py` 與 `identity_resolver.py`
- 所有任務派發操作（`initialize_mission`、`update_task`、`get_board`）遷移至此服務
- `ToukaMemory/server.py` 保持純記憶服務角色，移除所有任務派發工具

### 2. DISPATCH_TOKEN Bearer Token 認證

```python
def _check_token(token: str) -> bool:
    expected = os.environ.get("DISPATCH_TOKEN", "").strip()
    if not expected:
        return False  # 空 token 拒絕所有請求
    return secrets.compare_digest(token.strip(), expected)
```

規則：`DISPATCH_TOKEN` 為空時**拒絕全部**請求；無降級模式。

### 3. identity_resolver.py — 唯讀 SSOT 身份解析

> ⚠️ **已由 ADR-0047 取代（2026-05-26）**：ADR-0047 採用「身分固定化（Identity Fixation）」策略，所有 session 的 `agent_id` 永遠固定為 `"DispatchAgent"`，不再查詢 `identity_map.json`。`identity_resolver.py` 已移除，本節僅作歷史紀錄保留。

```python
def resolve_agent_identity(agent_id: str, map_path: Path) -> tuple[str, list[str]]:
    """從 identity_map.json 解析角色與能力，出錯時回傳 ("Base", [])。"""
```

- 純函式，只讀不寫
- 任何 I/O 或解析失敗均安全降級為 `("Base", [])` 而非注入特權
- `agent_id` 必須由呼叫方明確傳入，server 絕不自動推斷

### C1 修復：移除 FORCE_AGENT_ID 與 access_mission_board_tool

從 `ToukaMemory/server.py` 永久移除：
- `FORCE_AGENT_ID` 環境變數讀取邏輯
- `access_mission_board_tool` MCP 工具（含其行內 `FallbackIdentity` 類別）
- 憑證缺失時的自動能力注入邏輯

### C2 修復：_PathIdentity 模組級類別

將散落的行內 `FallbackIdentity` 替換為 `_PathIdentity` 模組級類別，集中定義於 `mission_manager.py` 頂部，確保全部路徑解析邏輯通過同一身份結構。

### C3 修復：角色大寫一致性

移除 `access_mission_board_tool` 的同時，消除了所有 `role="base"` (小寫) 的使用，系統改用 `role="Base"`（與 RBAC 矩陣一致）。

### H1 修復：BLOCKED 狀態必填 blocker_detail

```python
if status == TaskStatus.BLOCKED:
    if not blocker_detail:
        raise ValueError("blocker_detail is required when status is BLOCKED")
```

### H2 修復：validate_assignment=True

```python
class Task(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    ...
    @field_validator("artifact_ref", "artifact_refs", mode="before")
    @classmethod
    def _check_path_traversal(cls, v):
        # 現在對直接賦值亦觸發
        ...
```

---

## 後果 (Consequences)

### 正面
- C1/C2/C3/H1/H2 五個安全弱點全數修復
- ToukaDispatch 責任單一（SRP）：只處理任務派發
- identity_resolver.py 可獨立單元測試（純函式）
- `guard_all.py` 守衛數從 22 提升至 24（新增 lint_ssot 白名單修復 + 全體驗證）

### 負面
- 外部 AI 工具需額外設定 `DISPATCH_TOKEN` 才能存取任務看板（可接受：安全要求）
- `ToukaMemory/server.py` 不再提供 `access_mission_board_tool`（外部工具須改用 ToukaDispatch）

### 中性
- `LocalImportRule.check_FunctionDef` 在 `lint_ssot.py` 的潛在 bug（白名單不被套用）於本次同步修復
- 新增 `PATH_LITERAL_SSOT` 為第 18 個架構型 noqa 規則，總數更新至 134

---

## 測試覆蓋 (Test Coverage)

| 測試檔 | 測試項目 | 涵蓋弱點 |
|--------|---------|---------|
| `tests/unit/test_dispatch_server_tools.py` | 12 項 | C1, C2, C3, DISPATCH_TOKEN |
| `tests/unit/test_mission_manager_bugfixes.py` | 9 項 | C2, H1, H2 |
| `tests/unit/test_artifact_security.py` | 現有 | H2 路徑穿越防禦 |
| `tests/unit/test_status_transition.py` | 現有 | FSM, H1 |
| `tests/integration/test_mcp_dispatch_tools.py` | 3 項 | ToukaDispatch 端對端 |

全部 **870+** 測試通過，`guard_all.py` **24/24** 全綠。
