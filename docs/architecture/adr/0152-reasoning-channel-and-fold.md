# ADR-0152: 思考分頻與摺疊——reasoning 走獨立 SSE 頻道、前端預設收合顯示

## 狀態

✅ 已接受（hitl-unification 提案 T 系列 T1–T3；H 系列已於 ADR-0143 收官）（實作位置修訂 by [ADR-0156](0156-react-core-extraction.md)：reasoning 分頻/split_reasoning 移至 adapters、yield_reasoning_delta 移至 react_core；wire 合約與行為不變）

## 日期

2026-07-12

## 背景

Sumi（grok-4.5）症狀「思考內容漏進對話」（07-11 診斷，owner 目標②：思考預設摺疊、
點開可見）。診斷指出 openai_handler 只讀 `message.content` 逐字透傳、全路徑 grep
`reasoning` 零命中；claude_handler 則把 native thinking blocks 包成 `<thinking>` 文字
直塞 content。當時唯一無法讀碼證實的環節＝「grok-4.5 在傳統端點的思考實際格式」。

**T1-capture 實測定格式**（本案第一步，live 打 api.x.ai）：grok-4.5 傳統 chat
completions 回傳**獨立 `message.reasoning_content` 欄位**（含 `usage.completion_tokens_details
.reasoning_tokens`），content 本身乾淨無 think 標籤。＝漏出主因是 handler **丟棄**該欄位
且無分頻管道（前端只能看見混雜或缺失），而非 content 內嵌。內嵌 `<think>` 型仍存在於
DeepSeek-R1 蒸餾／Ollama 本地模型等，一併處理＝防禦縱深。

## 決策

**思考走獨立 SSE 頻道 `delta.reasoning_content`（沿用 DeepSeek 串流慣例欄位名），前端
以摺疊區塊顯示（預設收合、點開展開、串流中「思考中…」），並隨 session 持久化。**

wire 契約：OpenAI 相容 chunk 形狀不變、僅 delta 多一個可選欄位（additive）——舊前端／
其他消費者忽略未知欄位＝零破壞；伺服端既有消費者（heartbeat 轉錄、dispatcher 累積）
全用 `.get("content")`／`if "content" in delta` 讀取，reasoning chunk 安全略過＝心跳／
派工正文**正確排除思考**。

## 實作

### T1 後端分頻

- `src/utils/reasoning.py`（新）：`split_reasoning(text) -> (reasoning, clean)`——剝離
  **開頭**的 `<think>`／`<thinking>` 區塊（含未閉合＝整段思考）；正文中段的字面標籤
  不動（誤剝正文的代價高於漏剝思考）。
- `openai_handler`：訊息攝取點雙型分頻——①`message.pop("reasoning_content")`（grok-4.5
  ／deepseek-reasoner 欄位型；**pop＝ReAct 歷史 append 絕不回送**，DeepSeek 回送即 400）
  ②`split_reasoning(content)` 內嵌型，乾淨正文寫回 `message["content"]`（同一 dict 進
  歷史／final_response／防幻覺檢查＝全下游一致看乾淨正文）。思考以 `yield_reasoning_delta`
  先於正文發射（時序＝模型實際產出順序）。
- `claude_handler`：native thinking block 改走 `yield_reasoning_delta(block.thinking)`，
  不再以 `<thinking>` 文字塞 content；`all_thinking_and_tool_logs` 內部累積照舊（e_index
  ／記憶幻覺偵測行為不變）。
- gemini_handler 範圍外（提案明定；Gemini includeThoughts 另案）。
- 可恢復緩衝（ADR-0081 stream_buffer）在 **chunk 層級**原樣緩衝＝reasoning delta 天然
  隨重播恢復，零改動。

### T2 前端摺疊

- `connections.ts`：`dispatchSseLine`（fetch／resume 雙流共用解析 SSOT）解析
  `delta.reasoning_content` → 新 `onThinking` 回調（未接＝安全忽略）；`fetchChatStream`
  ／`resumeChatStream` 加尾端可選參數。
- `stream_tree.ts`：`applyThinkingToTree`（與 `applyChunkToTree` 同構、全量累積字串）；
  `tree_history.ts` `MessageNode` 加 `thinking?: string`。
- `StreamRegistryProvider`：`StreamState.aiThinking` 累積、`onThinking` 回調接兩條流；
  `persistDone` 存整棵樹 → **thinking 隨 session 自動持久化**（後端 `models/history.py`
  `MessageNode` 本有 `extra: "allow"`，後端零改動）。
- `ThinkingFold.tsx`（新）：預設收合、點開展開（`aria-expanded`）；串流中且正文未開始
  → 「思考中…」＋轉圈；低視覺權重（muted、純文字 pre-wrap，不走正文 Markdown）。
  `MessageItem` 於正文前渲染（時序一致）；i18n `chat.thinking.*`（zh-TW／en）。

## 驗證

- 單元：`test_split_reasoning`（9）＋`test_reasoning_channel`（4：欄位型／內嵌型／ReAct
  歷史乾淨／claude 分頻）＋前端 `stream_tree.thinking`（4）＋`connections.reasoning`（2）
  ＋`ThinkingFold`（3）。
- **Live E2E**（真 proxy＋真 grok-4.5）：SSE 實測 reasoning 61 字走 `delta.reasoning_content`、
  content＝`"399"` 乾淨、零 think 標籤＝**SPLIT OK**。
- 全套：後端 pytest／前端 vitest／guard_all（Guard #38 tool_events 封套不涉——reasoning
  為標準 chat chunk 非 tool_events）。

## 後果

- **正面**：思考不再漏進對話正文；前端可見可摺疊（預設收合）、跨重整／重開對話仍在；
  ReAct 歷史與防幻覺檢查看乾淨正文（省 token、少誤判）；deepseek-reasoner 類欄位型模型
  順帶受惠。
- **相容**：wire additive、舊消費者零破壞；claude 思考從「content 可見」改「摺疊區塊
  可見」＝顯示位置改變、資訊不減。
- **限制（誠實界定）**：①內嵌型只剝**開頭**區塊——模型在正文中段再開 think 標籤不處理
  （實測 grok-4.5 走欄位型、無此症；中段剝離誤傷正文風險更高）；②`split_reasoning` 於
  buffered 回應整段運作（openai_handler 非串流直連）＝無跨 chunk 斷標籤問題；若未來改
  上游串流透傳需另補跨 chunk 狀態機；③思考持久化明文存 session（與正文同等級；不另加密）。

## 相關

- [ADR-0143](0143-hitl-unification.md)（同提案 H 系列；T 系列至此**提案全數收官**）
- [ADR-0081](0081-resumable-stream-cursor-buffer.md)（可恢復緩衝——chunk 層級緩衝讓 reasoning 重播零改動）
- 診斷：[[touka-sumi-hitl-thinking-diagnosis]]（07-11 三根因之②）；提案
  `docs/architecture/proposals/hitl-unification-and-reasoning-fold.md`
