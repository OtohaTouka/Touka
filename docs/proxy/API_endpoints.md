# ToukaProxy API 端點完整文檔 (v2.6)
/
**版本**: 1.0.0  
**最後更新**: 2026年6月1日  
**更新說明**: 新增 Touka 原生創建引擎端點 `/v1/creation/{skill,mcp,scaffold}`（Owner/admin，ADR-0069）。前次：Skill 系統遷移至 Anthropic 標準 SKILL.md 目錄格式（ADR-0066）+ ToukaDispatch `auto_invoke` 自動召喚（ADR-0067）。

> 📌 **重要提示**：記憶系統工具（`memory_tools`, `vault_tools`, `project_tools`）已下沉至 ToukaMemory 模組，並依 ADR-0116 採**整合模式單一部署**，由 Proxy 同行程掛載。Proxy 扮演薄路由；工具內部的 `get_resource_proxy()` 只回傳注入的真實 `ResourceManager`，取不到即 fail-loud，不提供獨立 MCP 模式或自動 I/O fallback。更多詳情見 [記憶系統介面合約](../architecture/MEMORY_INTERFACE.md)。

---

## 📋 快速索引

### 核心服務
- `GET /` - 服務識別
- `POST /v1/auth/login` - 使用者登入
- `POST /v1/auth/logout` - 使用者登出 ⭐
- `POST /v1/auth/refresh` - Token 刷新
- `POST /v1/auth/change-password` - 自助修改密碼（驗當前密碼→清臨時旗標→撤銷 refresh token，DA25）⭐
- `GET /stats` - 統計數據
- `GET /emergence` - 湧現指標
- `GET /v1/outputs/{agent_id}/{filename}` - 主權資產服務 ⭐

### 靈魂管理
- `POST /souls` - 建立靈魂
- `GET /souls` - 列出靈魂
- `POST /souls/{soul_id}` - 更新靈魂

### 自定義模型管理 (Level 2) ⭐
- `GET /v1/custom_models` - 列出所有自定義模型 (type: custom)
- `GET /v1/custom_models/{model_id}` - 取得模型詳情
- `POST /v1/custom_models/{model_id}` - 建立或更新模型
- `DELETE /v1/custom_models/{model_id}` - 刪除模型

### AI 代理
- `POST /v1/messages` - Anthropic 相容 (推薦) ⭐
- `POST /v1/chat/completions` - OpenAI 相容

### 專案管理 (UI Projects) ⭐
- `GET /ui/projects/list` - 列出所有專案
- `POST /ui/projects/save` - 建立或同步新專案 (ProjectModel)
- `DELETE /ui/projects/{project_id}` - 刪除專案標記
- `GET /ui/projects/{project_id}/overview` - 取得專案概觀 (審計、參與者、相關對話)
- `GET /ui/projects/{project_id}/files` - 取得專案檔案樹 (支援 4 層遞迴)

### 編輯器服務 (Editor Service)
- `GET /editor/files` - 列出工作區實體檔案 (用於 @Mentions)
- `GET /editor/read` - 讀取實體檔案內容

### 系統設定 (UI Settings) ⭐
- `GET /ui/settings/workspace` - 獲取主機工作區路徑設定
- `POST /ui/settings/workspace` - 更新主機工作區路徑設定

### 技能、權限與身分管理 (New) ⭐
- `GET /v1/skills/list` - 列出所有已註冊技能（Anthropic 標準 SKILL.md 目錄格式，ADR-0066）
- `GET /v1/skills/{skill_id}` - 取得單一技能詳情
- `POST /v1/skills` - 建立或更新技能
- `DELETE /v1/skills/{skill_id}` - 刪除技能（`is_system` 受保護）
- `GET /v1/skills/{skill_id}/export` - 打包技能為 `.skill`（ZIP）下載 ⭐ *ADR-0066*
- `POST /v1/skills/install` - 由 URL 安裝 `.skill`（Owner/admin，SSRF 白名單 + zip-bomb 防護）⭐ *ADR-0066*
- `GET /v1/permissions` - 獲取當前分級權限設定 (SSOT)
- `POST /v1/permissions` - 更新分級權限設定
- `GET /v1/identity` - 獲取身分與能力映射 (Identity Map SSOT)
- `POST /v1/identity` - 更新身分與能力映射 (含心跳喚醒特權)
- `GET /v1/identity/agents` - 列出所有已發現的實體 Agent 清單

### 創建引擎 (Creation Engine) ⭐ *ADR-0069*
- `POST /v1/creation/skill` - 創建 skill（Owner/admin；走 VALIDATE→SMOKE→COMMIT→EVAL 流水線）
- `POST /v1/creation/mcp` - 註冊 MCP server（Owner/admin）
- `POST /v1/creation/scaffold` - 決定性生成 FastMCP server stub（Owner/admin；可選 `activate` 直接餵入創建流水線）
> 驗證/SMOKE 失敗回 **200 + `errors[]`**（產物未上線，不報 500）；無 token→401、錯角色→403、多餘欄位（`extra=forbid`）→422。CREATE 不覆寫既有 id。

### 可觀測性 (Observability)
- `GET /health` - 健康檢查
- `GET /system_health` - 系統資源狀態
- `GET /memory` - 記憶系統載入狀態
- `GET /tools` - 完整可用工具清單 (包含 vault_tools, project_tools)
- `GET /tools_stats` - 工具 JSON Schemas 詳情
- `GET /logs` - 代理 Payload 日誌
- `GET /v1/models` - 全部可用模型 (SSOT: 支援 ID 歸一化與舊 ID 映射糾偏) ⭐

### 錯誤處理 (Error Handling) ⭐
- 所有非 200 回應皆遵循 `ToukaException` 結構：
  ```json
  {
    "detail": {
      "code": "ERROR_CODE",
      "id": 1001,
      "message": "Human readable",
      "i18n": "error.key"
    }
  }
  ```
- 錯誤定義來源 (SSOT): `errors.json`

---

## 專案管理 API

### `GET /ui/projects/list`
回傳系統中所有已註冊的專案列表。包含專案 ID、名稱、Root Path 以及關聯的 Session 計數。

### `POST /ui/projects/save`
註冊或更新一個專案資訊。
**Payload (ProjectModel)**:
```json
{
  "id": "project-id",
  "name": "MyProject",
  "root_path": "D:/Touka/MyProject",
  "description": "Optional desc",
  "updatedAt": 171XXXXX
}
```

---

## 工具系統變動 (Alias Proxies)

從 v0.5.0 開始，核心檔案操作工具已解耦為：
1.  **`vault_tools`**: 用於管理 `personal`, `legacy`, `soul` 檔案庫。
2.  **`project_tools`**: 僅在對話關聯專案時動態注入，用於操作專案源碼。
兩者皆支援 `action="grep"`, `"find"`, `"list"`, `"read"`, `"write"`, `"str_replace"`。

---

## 更新歷史

| 2026-06-06 | 1.0.0 | **深度對抗式審計修復（DA1–DA28）**: 新增 `POST /v1/auth/change-password`（DA25）；`/v1/admin/users*` 四端點改 `response_model=UserOut`（DA22，移除 bcrypt 雜湊＋refresh JTI）；帳號停權強制（DA23）、心跳 `/awaken` 加固（DA24）、debug/souls 跨用戶 IDOR（DA17）、演化沙箱 OS 級資源限制（DA28/ADR-0076）等。詳見 [SECURITY.md](../../SECURITY.md)。 |
| 2026-06-01 | 1.0.0 | **創建引擎對外開放（ADR-0069）**: 新增 `POST /v1/creation/{skill,mcp,scaffold}`（Owner/admin，`_require_creation_role`）。route 與 agent 工具共用薄門面 `services/creation/api.py`；走 VALIDATE→SMOKE→COMMIT→EVAL 流水線；驗證/SMOKE 失敗回 200+errors（不報 500）。新增 Creation Sovereignty Guard #29。 |
| 2026-05-31 | 1.0.0 | **Skill Anthropic 標準化 + Dispatch 自動召喚**: 技能遷移至目錄式 `SKILL.md` + `.mcp.json`（Progressive Disclosure、Pull auto-trigger、`.skill` 打包）。新增 `GET /v1/skills/{id}/export`、`POST /v1/skills/install`。`dispatch_initialize_mission` 加 `auto_invoke` 依 `dispatch_hooks.json` 白名單自動召喚外部 AI（ADR-0066 + ADR-0067）。 |
| 2026-05-10 | 0.9.8 | **安全會話與部署等價性**: 實作 `/v1/auth/logout` 與 Cookie 路徑統一。建立 `StorageService` 與 `ProxyClient` 將 Heartbeat 組件解耦。在 `guard_dependencies.py` 實作「Dockerfile 物理完整性檢查」。 |
| 2026-05-06 | 0.9.2 | **環境與錯誤碼主權**: 遷移至 Pydantic Settings。實施 `errors.json` 跨端同步。部署 `lint_ssot.py` (後端) 與 `guard_errors.ts` (前端) 聯合執法。 |
| 2026-05-05 | 0.9.1 | **API 合約校驗與時間主權**: 實作 `guard_frontend.ts` 強制校驗前後端路徑一致性。全面遷移至 `/v1` 標準路徑。實作 `guard_time.ts` (前端) 與 `lint_ssot.py` (後端) 聯合執法時間主權。重構心跳白名單為身分能力機制。 |
| 2026-05-04 | 0.9.0 | **I/O 代理化與串流等價性**: 補全 `ResourceManager` 二進位與追加介面。補全 `AnthropicRequest` 串流旗標，修正 ReAct 整合測試。 |
| 2026-04-24 | 0.8.0 | **身分主權全面滲透**: 修正 `/ui/custom_models` 等路由與 Middleware 的 `AgentIdentity` 物件化依賴。 |
| 2026-04-24 | 0.7.8 | **大腦控制大一統**: 新增系統內建工具 `agent_control` 用於管理大腦切換與狀態持久化。 |
| 2026-04-09 | 0.7.0 | **主權資產路由 (Sovereign Outputs)**: 實作 `/v1/outputs/{agent_id}/{filename}`。強制 Agent 產出物隔離於私有 Vault 中，廢除全域 `data/outputs`。 |
| 2026-04-09 | 0.6.6 | **身分識別收斂**: 實作 `AgentService.identify_agent()` 統一入口。實作 `ResourceManager` 路徑監獄原則。 |
| 2026-03-31 | 0.6.5 | **自定義模型 (Level 2) SSOT**: 新增 `custom/` 目錄與模組化模型分級。實作 `CustomModelLayer` 注入與 SSOT 發現。 |
| 2026-03-23 | 0.5.0 | **專案整合與安全鎖定**: 新增專案 CRUD APIs。實作 Alias Proxy 工具架構與萬能檔案引擎 `core_file_ops`。 |
| 2026-03-21 | 0.4.3 | **新增 MCP 管理**: 新增 MCP 配置讀寫端點與工具數統計列表。 |
| 2026-03-20 | 0.4.2 | **補全缺失端點**: Health, System, Memory, Tools, Reject APIs。 |
