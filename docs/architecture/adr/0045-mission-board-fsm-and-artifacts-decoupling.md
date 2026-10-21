# ADR-0045: Mission Board FSM Transition Validation and Artifacts Decoupling — 任務看板有限狀態機校驗與產出物欄位解耦

**狀態**: ✅ 已接受（實作修訂 by [ADR-0141](0141-role-plane-separation.md)：§2 具名 identity.role 管理者組檢查→identity.is_privileged()（平面 role 欄已拆為 human_role/agent_role）；特權集合行為不變）
**日期**: 2026-05-26
**關鍵字**: TaskStatus, FSM, Valid Transitions, Reset FAILED, Privilege Restriction, artifact_ref, artifact_refs, Decoupling, response_model, model_dump

---

## 背景與問題

隨著自主任務看板 (Mission Board) 與外部 AI Coding 工具（例如 Clin/Roo-Code、Antigravity CLI 等）緊密整合，任務流轉的規範性、安全防禦性以及資料型別的確定性遇到了以下關鍵挑戰：

1. **狀態流轉缺乏有限狀態機 (FSM) 約束**：
   原有任務看板在更新子任務狀態時，允許任意狀態直接跳轉（例如 `TODO -> DONE` 或 `DONE -> TODO`）。這使任務看板容易被外界工具隨意踩踏，甚至跳過必要的執行步驟（`DOING`），破壞了進度追蹤的邏輯一致性。
2. **FAILED 任務的重置主權缺失**：
   外部 AI coding 代理人在執行任務失敗並使狀態進入 `FAILED` 後，不應允許其隨意自行將任務重新改回 `TODO`，而應當由系統擁有者（Owner/Debugger/admin）或特權監管身份（具有 `can_supervise_dispatch` 能力的 Touka Agent）審查完回報結果與看板日誌後，手動或特權發起重設。
3. **產出物欄位 (Artifacts) 型別混用與覆蓋踩踏**：
   原先的設計中，為了支持單一產出物 `artifact_ref` (str) 與多產出物變更列表 `artifact_refs` (list[str])，兩者在物理儲存上混用並踩踏同一個 `Task.artifact_ref` 屬性（其型別定義為 `str | list[str] | None`）。這造成了兩大問題：
   - **覆蓋踩踏**：不同呼叫來源若分別傳入單一值或列表，會踩踏並覆蓋彼此已存的資料。
   - **前端判定複雜化**：前端 TypeScript 渲染時，必須不斷通過 `isinstance` / `Array.isArray` 對該屬性進行型別判斷，型別混用不一致極易引發渲染崩潰或型別宣告混亂。
4. **API 響應序列化與合約衝突**：
   FastAPI 的 `/ui/mission` 路由宣告回傳 `dict[str, Any]`，但其實作直接回傳了 `MissionState` Pydantic 模型實體。在正常運作中（查無任務回傳 404）不會暴露，但一旦任務檔案存在並回傳實體時，FastAPI 檢測到回傳值非標準 dict 字典，會直接拋出 `ResponseValidationError` 導致 500 Internal Server Error 崩潰。

---

## 決策

為了解決狀態機失控、特權重設防禦、型別混用與 API 序列化崩潰，我們實施了以下架構重構：

### 1. 任務有限狀態機 (FSM) 轉換矩陣定義
在 `MissionManager` 中定義嚴格的狀態流轉矩陣：
- **`TODO`** ─> `{DOING, BLOCKED}` (必須先經過 `DOING` 執行，不允許直接 `DONE`)
- **`DOING`** ─> `{TODO, DONE, FAILED, BLOCKED}`
- **`BLOCKED`** ─> `{TODO, DOING}`
- **`FAILED`** ─> `{TODO}` (限定特權身分重設)
- **`DONE`** ─> `{}` (終端狀態，封鎖後續任何修改)
- 相同狀態（如 `DONE -> DONE`）流轉判定為冪等，不進行任何檢查直接成功放行。

### 2. FAILED 任務的特權重設限制 (Privilege Verification)
- 當目前任務狀態為 `FAILED` 且請求更新為 `TODO` 時，`MissionManager` 會強制查驗調用者的身分 (Identity)：
  - 只有 `identity.capabilities` 包含 `"can_supervise_dispatch"` 能力，或者 `identity.role` 屬於管理者組 `{"Owner", "Debugger", "admin"}` 的 Touka Agent 才可執行。
  - 外部 AI 代理人等無特權身分自己發起重置時，直接予以拒絕並拋出 ValueError。

### 3. 產出物欄位 `artifact_ref` 與 `artifact_refs` 的徹底解耦
- 在 `Task` (Pydantic 模型) 中將兩者徹底拆分為兩個獨立、型別確定且不混用的屬性：
  - `artifact_ref: str | None = None`
  - `artifact_refs: list[str] = Field(default_factory=list)`
- 在 `update_task` 中解耦寫入逻辑，各自覆蓋各自的欄位，互不干涉。
- 在 `validate_artifact_ref` 驗證器中，分別對這兩個欄位進行獨立的絕對路徑及路徑穿越防禦性校驗。
- 後端 `server.py`、`mission_tools.py` 與前端 UI 看板同時對兩個欄位進行獨立格式化顯示。

### 4. API 序列化與合約對齊
- 將 `/ui/mission` 路由的回傳值改為 `mission.model_dump()`，將 `MissionState` 模型轉為標準的 Dict，與 `dict[str, Any]` 的路由回傳宣告完美對齊，徹底消除全量測試時的 `ResponseValidationError` 500 錯誤。
- 執行 `python scripts/update_openapi.py` 重新生成並合併 `openapi.json`，對齊前端 TypeScript 介面定義。

---

## 影響

- **防範非法狀態流轉**：禁止了任務在 `TODO` 狀態下直接越過執行宣告為 `DONE` 的作弊行為，保證了任務看板的執行嚴肅性。
- **維護特權重置主權**：外部 AI coding agent 無法在失敗時自我掩蓋或重置看板，必須由 Touka Agent 審查日誌後手動重設。
- **實現型別安全與解耦**：產出物欄位型別 100% 確定，不再有混用型別踩踏與覆蓋衝突，降低了前後端處理與 TypeScript 對齊的複雜度。
- **系統 100% 穩定綠燈**：徹底解決了全量 pytest 執行時的 500 異常，測試套件 (829 Passed) 與 26/26 守衛全數綠燈通過。
