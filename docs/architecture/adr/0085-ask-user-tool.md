# ADR-0085: AI 詢問使用者工具（ask_user，人在迴圈中的暫停-續跑）

**狀態 (Status):** ✅ 已接受
**日期 (Date):** 2026-06-13
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

Microsoft HAX 走查的 **G10（存疑時收斂服務）** 指出：AI 不確定、需使用者決定時，目前只能猜或
硬做——僅靠 AuthSwitcher 的 Manual 被動收斂，沒有「主動發問」管道。本 ADR 新增 `ask_user` 工具：
AI 生成中可呼叫它向使用者提出結構化問題（**單選／複選／連續多問題，每題可附「其他」自由填**），
**暫停生成、等回答、再以答案續跑**。屬 HAX 後續批次（G10）。

維護者決定：① 連續多問題＝一頁顯示全部（一次送出）；② 每題允許「其他」自由填。

## 決策方案 (Decision)

**高度重用既有「人在迴圈中」骨架**，幾乎不造新輪子：
- **工具執行合約**（`services/tool_executor.py`）：每個工具收 `_emit_event`（推自訂事件進對話 SSE）、
  `identity`、`_call_id`；**回傳字串即 AI 的 tool_result**，AI 據此續跑。
- **暫停-等待原語**：新 `services/ask_user_store.py` 照抄 `dispatch_callback_store`（ADR-0051）的
  token→`asyncio.Event`→`deliver`/`wait` 模式。
- **事件→彈窗→回傳**：照 `request_skill_install` → `connections.ts` customEventTypes →
  `StreamRegistryProvider.onEvent` → 回調 → modal → POST 的同一條線。

### 流程
1. AI 呼叫 `ask_user(questions=[{question, options, multiSelect, allowOther?, header?}])`。
2. 工具 `await _emit_event({type:"ask_user_request", id:token, questions})` → 對話串流 → 前端彈窗。
3. 工具 `await ask_user_store.wait_for_answer(token, timeout=600)`（生成自然暫停）。
4. 前端 `AskUserModal` 作答 → `POST /v1/ask_user/answer/{token}` `{answers:[[...],…]}` →
   `ask_user_store.deliver_answer` → 解除等待。
5. 工具回傳格式化答案（`[{question, answer:[…]}]`，含「其他：…」）；逾時則回「使用者未在時限內回答，
   請在無此資訊下繼續」哨兵，AI 不卡死。

### Headless 安全閘門
`middleware/layers/tool.py` 注入工具清單時，**僅互動式（UI 對話）請求才注入 `ask_user`**——以
`ctx.get("resumable_stream")`（proxy.py 僅在 UI 對話端點設，ADR-0081）判定。心跳/SNS 自主情境
不給此工具 → AI 不會呼叫 → 不會掛住。（Owner 角色 allowed_tools=`["*"]`，互動情境即注入。）

### 續傳
`ask_user_request` 走對話 SSE → ADR-0081 串流緩衝重播 → 重整後問題彈窗自動重現（token 仍在 store）。

## 後果與影響 (Consequences)

- 新增隔離的 `ask_user_store`（in-memory，server 重啟後待答失效，沿用 ADR-0081 限制）；
  新增 `ask_user` 工具＋schema＋registry；新增 `POST /ask_user/answer/{token}`（有 auth，過 Guard #24）；
  `openapi.json`＋`api.ts` 重生（Guard #17）；`NOQA_REGISTRY.md` B008 隨新 `Depends` 上修（Guard #19）。
- 預設 timeout 600s（逾時 AI 續跑）；沿用 tool_executor／串流既有管線，零改 handler。
- 殘留（記錄不做）：開放式（非選項）提問流、多輪精靈式（本批一頁全部）、server 重啟後待答持久化。

## Action Items
1. [x] `ask_user_store`（asyncio.Event 暫停-續跑）＋ TDD（deliver/wait/timeout/並發）。
2. [x] `ask_user` 工具＋schema/registry；ToolLayer 互動閘門；答案端點＋ auth。
3. [x] 前端 `AskUserModal`（單選/複選/其他、一頁全部）＋事件接線＋POST。
4. [x] 契約同步（openapi/api.ts）＋ NOQA 登記＋ `hax_basics` 守衛擴充＋全量驗證。
