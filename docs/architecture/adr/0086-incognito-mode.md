# ADR-0086: 無痕模式（Incognito，零殘留對話）

**狀態 (Status):** ✅ 已接受
**日期 (Date):** 2026-06-13
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

Microsoft HAX 走查的 **G17（提供全域控制）**：Touka 以持久記憶為招牌，卻缺一個「這次對話不留痕」
的選項。本 ADR 新增**無痕模式**——被標記無痕的對話**任何持久化都不發生**：不存 session、AI
**完全失憶**（既不讀既有記憶/家對話、也不寫新記憶）、不寫 payload 日誌；前端只存記憶體、不寫
localStorage、關閉即丟、視覺明顯標示。維護者決定：**完全失憶**（讀寫記憶皆關）。

**核心原則**：「零殘留」是全有全無——漏一個寫入點承諾即破。故採**雙層防禦**（前端不送＋後端不寫）。

## 決策方案 (Decision)

### 旗標傳遞（單一事實：request body → ctx）
- `adapters/schemas.py` 的 `OpenAIChatRequest`/`AnthropicRequest` 加 `incognito: bool = False`（走 body）。
- `models/context.py` 的 `PipelineContext` 加 `incognito: bool = False`。
- `routers/proxy.py`：兩對話端點在 `if identity:` 區塊（resumable_stream 設定處）加 `ctx["incognito"] = request.incognito`。
- 前端：`Session.incognito` → `StartStreamParams.incognito` → `connections.ts` body `incognito`。

### 後端四閘門（皆 ctx/incognito 早退）
| 寫入點 | 位置 | 閘門 |
|---|---|---|
| Session 落盤 | `dispatcher._pump_stream_to_buffer`（spawn 處有 context） | 傳入 `incognito`，跳過 `_persist_assistant_turn` |
| 記憶寫入 | `middleware/layers/tool.py`（memory schema 注入） | 無痕時不注入 memory_tools（AI 無從讀寫記憶） |
| 記憶讀取/家對話 | `middleware/layers/soul.py`（呼叫 `inject_home_messages` 處） | 無痕時跳過 `inject_home_messages`（保留人格、只失憶） |
| Payload 日誌 | `proxy_service.process_proxy_request`（`log_payload` 呼叫） | 無痕時跳過 `log_payload` |

### 前端雙層防禦＋UX
- **單一收口防線**：`SessionListProvider.syncSession` 頂部 `if (sess.incognito) return;`——所有持久化
  呼叫點 funnel 此處，一閘擋盡。旗標經 `StreamRegistry`（`persistDone`/user 回合先行 sync 皆跳過）、
  `ChatProvider.handleNewSession`、`useChatStream`、`connections.ts`（body 帶 incognito 觸發後端閘門）。
- UI：`LeftSidebar` 加「無痕對話」入口；`SessionItem` 無痕標籤；全域匯出（`useSessionEvents`）過濾無痕。

### 邊界
- **N/A（不適用無痕）**：SNS gateway、heartbeat 自主迴圈（無真人、無前端，本就全程持久化）。
- **可接受殘留（記錄不關）**：stream_buffer（in-memory TTL 600s，server 重啟即滅）；system_status.log
  錯誤訊息可能含片段（營運日誌不全關；payload 日誌才是內容承載者，已關）。
- **不在範圍**：停用工具副作用（bash/檔案）——無痕只管「對話/記憶」持久化，工具照常（那是使用者的行動）。

## 後果與影響 (Consequences)

- 新增 `incognito` 旗標貫穿 schema/context/proxy；四個後端閘門＋前端 syncSession 收口；
  `openapi.json`＋`api.ts` 重生（Guard #17）。端點 auth 不變（Guard #24）。
- 完全失憶：無痕對話中 AI 不引用既有記憶/家對話、不寫新記憶——可見行為變化（擁有者已知）。
- 以 integration 守衛證明「無痕請求 → `log_payload` 未呼叫」＋ dispatcher 落盤閘門單元測試證明零落盤。

## Action Items
1. [x] 旗標貫穿（schema/context/proxy）＋四後端閘門＋ TDD（dispatcher 落盤、tool memory、log、零殘留 integration）。
2. [x] 前端 syncSession 收口防線＋旗標流＋connections body＋匯出過濾。
3. [x] UI：LeftSidebar 無痕入口＋SessionItem 標籤。
4. [x] 契約同步（openapi/api.ts）＋ `hax_basics` 守衛擴充＋全量驗證。
