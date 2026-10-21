# ADR-0056: Test Mock Type Alignment — 測試 Mock 與型別定義對齊

> **現況**：mock 加 `hashed_password` 的做法作廢：User 已沒有這個欄位（DA22 安全修訂）。

**狀態**：✅ 已接受（安全修訂 by DA22 InfoSec 修復：User=UserOut 已無 hashed_password 欄，測試 mock 現正確省略——**讀者勿照文中「每個 User mock 加 hashed_password」重新引入被移除的安全欄位**（TS 現會拒）；型別對齊原則不變、守衛 test_admin_userout_security.py 執法）  
**日期**：2026-05-29  
**影響範圍**：ToukaChat 測試檔案、vitest.config.ts  
**關鍵字**：TypeScript, test-mocks, type-drift, vitest, poolOptions, SoulStudio

---

## 背景

執行 `npx tsc --noEmit`（含測試檔案的主 tsconfig）發現 9 個測試檔案共 30+ 個 TypeScript 型別錯誤。這些錯誤的共同根因是**型別漂移（Type Drift）**：測試 Mock 物件在 ADR 迭代過程中逐漸不符合對應的 Pydantic/TypeScript 型別定義，但因 vitest 使用 esbuild 轉譯（跳過型別檢查）而靜默通過。

---

## 問題清單（9 個檔案）

| 檔案 | 錯誤類型 | 根本原因 |
|------|----------|---------|
| `SoulStudio.test.tsx:108` | TS2493 tuple 無第3元素 | `vi.hoisted` mock 只宣告2個參數，但呼叫方傳入3個 |
| `McpTab.test.tsx` (6處) | TS2322 / TS2339 | Mock 缺少 `McpServerStatus.status`、`url`；`nameInput` 未縮窄至 `HTMLInputElement` |
| `SecurityTab.test.tsx` (5處) | TS2322 | Mock 缺少 `PermissionConfig.agent_mappings`、`system_config`；`IdentityMap.tag_rules`；`Tool.description`；`McpServerStatus.status`/`url`；`Skill` 多個欄位 |
| `useSetupStatus.test.ts` (5處) | TS2741 | 所有 User mock 缺少必填欄位 `hashed_password` |
| `connections.logout.test.ts:60` | TS2345 | 反變型別：`([url]: [string])` tuple 不可指派至 `(value: any[]) => unknown` |
| `AuthGuard.test.tsx` (3處) | TS2345 | `AuthContextType` mock 缺少 `user`、`refreshUser` 欄位 |
| `AuthPages.test.tsx` (2處) | TS2345 | 同上 |
| `connections.security.test.ts` (5處) | TS2304 / TS7031 | `vi` 未從 vitest import；`mock.calls` 隱式 any |
| `useAgentSSE.test.ts` (2處) | TS2339 | `ChatMessage` 為聯合型別，`steps` 屬性只在特定成員存在，需縮窄後才可存取 |

---

## 決策

### 1. 修復各測試檔案的 Mock 型別（SSOT：型別定義為唯一真相來源）

每個 Mock 物件必須與其對應的 TypeScript 介面或 Pydantic 模型完全對齊。禁止用 `as any` 繞過型別錯誤；需補全所有必填欄位。

### 2. 新增 `tests/unit/test_test_file_typescript.py`（TDD 結構性測試）

```python
# 確保 9 個目標測試檔案不再有 TypeScript 錯誤
# Red → Green 驗證已完成
```

### 3. `vitest.config.ts` 新增 `testTimeout` 和 `poolOptions`

**問題**：`SoulStudio.test.tsx` 的整合式測試在 54 個 worker 並行時，`handleCreateSoul` 的 async 鏈（`fetchTouka` → `refreshAgents` → `loadSouls`）無法在 vitest 預設 testTimeout 5000ms 內完成。

**決策**：
- `testTimeout: 15000`：測試執行上限從預設 5000ms 提升至 15000ms
- `pool: 'forks'` + `maxForks: 4`：以 subprocess fork 取代 thread，並限制並行數至 4，減少 CPU 競爭

**選用 forks 而非 threads 的原因**：Node.js worker threads 共享記憶體空間，在高並行時存在更多 GC 競爭。Forks 模式每個 worker 為獨立 process，I/O 隔離性更好，適合大量 jsdom + React 渲染的測試。

---

## 型別修復細節

### `McpServerStatus` 必填欄位
```typescript
// 修復前
{ name: "memory", tools_count: 5 }
// 修復後
{ name: "memory", tools_count: 5, status: 'connected' as const, url: 'http://localhost:3001' }
```

### `PermissionConfig` 必填欄位
```typescript
// 修復前（缺少 agent_mappings、system_config）
permissions: { roles: { ... } }
// 修復後
permissions: {
    roles: { ... },
    agent_mappings: {},
    system_config: { default_role: "guest", allow_unregistered_agents: false }
}
```

### `AuthContextType` 必填欄位（AuthGuard + AuthPages）
```typescript
// 修復前（缺少 user、refreshUser）
{ isAuthenticated: true, accessToken: '...', login: vi.fn(), logout: vi.fn() }
// 修復後
{ isAuthenticated: true, accessToken: '...', user: null, login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn() }
```

### `User` 必填欄位（useSetupStatus）
```typescript
// 修復前（缺少 hashed_password）
{ role: 'admin', email: 'admin@test.com', id: '1', status: 'active' }
// 修復後
{ role: 'admin', email: 'admin@test.com', id: '1', status: 'active', hashed_password: '$2b$12$test' }
```

### `ChatMessage` union 型別縮窄（useAgentSSE）
```typescript
// 修復前（TypeScript 不知道 messages[1] 有 steps）
expect(result.current.messages[1].steps).toHaveLength(1);
// 修復後（縮窄至 agent role）
const agentMsg = result.current.messages[1] as { role: 'agent'; steps: ToolStep[] };
expect(agentMsg.steps).toHaveLength(1);
```

---

## SSOT 對齊

| 元素 | SSOT 來源 |
|------|-----------|
| Mock 必填欄位 | TypeScript 介面（`lib/types.ts`、`contexts/AuthContext.tsx`、`types/api.ts`） |
| 測試型別正確性驗證 | `tests/unit/test_test_file_typescript.py` |
| 測試執行設定 | `ToukaChat/vitest.config.ts` |

---

## 後果

### 正面
- `npx tsc --noEmit` 對所有測試檔案零型別錯誤
- TDD 結構性測試確保未來 Mock 漂移即時被偵測
- vitest `pool: forks` + `maxForks: 4` 提升測試穩定性
- 204/204 vitest 測試穩定通過（3次連續確認）

### 負面 / 權衡
- `hashed_password: '$2b$12$test'` 在 User mock 中是一個測試用占位符，非真實 bcrypt hash
- `pool: 'forks'` 消耗更多記憶體（每個 fork 是獨立 process），但測試穩定性更高
- `testTimeout: 15000` 延長了失敗測試的回饋時間
