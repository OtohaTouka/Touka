# P1 實作計畫：統一工具事件封套（Tool-Event Envelope SSOT）

> 狀態：**✅ 已實作並驗證通過（2026-07-04）、收斂為 [ADR-0122](../adr/0122-tool-event-telemetry-envelope.md)（2026-07-05）。** 維護者決定：P1-D 納入、`duration_ms` 不附 `log`、guard 不管泛型 `error`。四子項 A→B→C→D 全數 TDD 完成。本文件是
> [`tool-observability-and-reliability.md`](tool-observability-and-reliability.md) §6 **Phase 1** 的細部 TDD 拆解。
> 約束：**由維護者 commit**；只用 **DebugAgent** 驗證、不動其他 agent 資料；**嚴禁自動修改 `data/`**；
> 每步過 ruff＋lint_ssot＋guard_all＋pytest＋vitest。
> 前置：**P0「量尺」已於 2026-07-04 完成**（pass^k／abstention／token 成本就位），P1 的改動都可用這把尺確認「沒有退化」。

---

## 1. 目標與範圍（先界定「不是什麼」）

**一句話**：把目前四散、各自為政的工具生命週期事件（`tool_start` / `tool_stream` / `tool_result` / `error`）收斂成**單一封套 SSOT**——**一份事實、兩個投影**（模型面結構化 × 人面 SSE），屬性命名對齊 **OpenTelemetry GenAI semantic conventions**，並把目前缺的 `duration_ms`、被埋在字串裡的 `error_class` 補成**一級屬性**。

### P1 **是**
- 一個新的封套模組 `src/utils/tool_events.py`（純函式 builder ＋ 規範 record dataclass），與既有 `progress_events.py` 同一風格、同一目錄。
- 把 `tool_executor.py`／`terminal.py`／`interaction.py` 裡的**臨時 dict 字面量**改為經此 SSOT 產生（輕度重構）。
- **補兩個一級屬性**：`duration_ms`（工具執行耗時，目前完全沒量）、`error_class`（目前埋在 `result_text` 的 JSON 字串裡，抬成事件屬性）。
- 一支**事件一致性 guard**（靜態檢查未來新增的工具事件必須走 SSOT）。

### P1 **不是**（寫死避免範圍蔓延）
- **不改任何 wire 欄位名**。前端 `connections.ts` 的 `customEventTypes` 與 `stream_tree.ts` 依賴的 `id/tool/args/status/result/chunk/message/kind` **全部凍結**；新屬性一律**加法式（additive）**附加（前端忽略未知鍵）。**前端在 P1 零改動**——人面卡片重整是 **P3**。
- **不寫外部 OTel exporter／spans**（接 Datadog／Phoenix 那些是 **P4**）。P1 只借 OTel 的**命名詞彙**，不引 SDK、不建立 span。
- **不擴入回合層遙測**。`tokens` / `finish_reason` 屬於 LLM 回合（已由 `progress_events.token_usage_event` 承載），P1 只在對照表登記共用詞彙，**不把它們塞進工具事件**。
- **不動 `touka_progress`**。它早已是統一外層（`type=touka_progress` + `kind` 判別），P1 只在文件把它納入同一詞彙表，程式零改。
- **不碰 `log` / `touka_status`**。這兩者是人面 chrome、非工具遙測，維持原樣。

---

## 2. 現況精確盤點（file:line）

### 2.1 後端事件誕生點（皆為臨時 dict 字面量）
| 事件 | 位置 | 現形狀 |
|---|---|---|
| `tool_start` | `src/services/tool_executor.py:233` | `{"type":"tool_start","id":call_id,"tool":name,"args":args}` |
| `tool_result` | `src/services/tool_executor.py:358-365` | `{"type":"tool_result","id":call_id,"result":result_text,"status":"success"/"error"}` |
| `error` | `src/services/tool_executor.py:378-379` | `{"type":"error","id":call_id,"message":detail}` |
| DENY 的 `tool_result` | `src/services/tool_executor.py:216-218` | 同上（status=error） |
| `tool_stream` | `src/tools/terminal.py:239` | `{"type":"tool_stream","id":call_id,"chunk":text}` |
| `ask_user_request` | `src/tools/builtin/interaction.py:44` | `{"type":"ask_user_request","id":token,"questions":questions}` |

- **無中央 SSE 序列化 helper**：三個 handler（`claude_handler.py`、`gemini_handler.py`、`openai_handler.py`）各自硬編 `yield f"data: {json.dumps(event)}\n\n"`，迴圈邏輯三份相同（僅 `log` 被抽出轉 `yield_delta`，其餘 passthrough）。
- **無事件型別註冊表**：靠「後端字串 ↔ 前端 `customEventTypes`」的隱性約定。
- **`error_class` 埋在字串**：錯誤時 `result_text` 是 `tool_errors.build_tool_error(...)` 的 JSON，`is_tool_error()` 只做前綴偵測；類別本身沒有抬成事件屬性。
- **無 duration**：`execute_to_queue` 完全沒有計時。

### 2.2 前端凍結契約（P1 不得破壞）
- 型別註冊：`ToukaChat/src/lib/connections.ts:356` `customEventTypes = ["touka_status","tool_start","tool_result","error","loop_status","skill_install_approval_request","ask_user_request","touka_progress","tool_stream"]`。
- 消費：`ToukaChat/src/lib/stream_tree.ts:142-164` `applyEventToTree`：
  - `tool_start` 讀 `id/tool/args` → 建 `ToolStep{status:'running'}`。
  - `tool_result` 讀 `id/status/result`（`status==='error'` → 紅，否則綠）。
  - `tool_stream` 讀 `id/chunk`（append 到 `step.result`）。
  - `error` 讀 `id/message`。
- 型別：`ToukaChat/src/lib/tree_history.ts:3-9` `ToolStep{id,tool,args,status:'running'|'success'|'error',result?}`。
- 配色 token：`text-status-success`／`text-status-error`（`ToolStepTracker.tsx`）。
- **結論**：新屬性（`duration_ms`／`error_class`）以 additive 附加即可，前端 `?.` 容忍未知鍵，UI 不受影響；等 P3 再消費它們。

---

## 3. 設計：一份事實、兩個投影

新模組 **`src/utils/tool_events.py`**（純函式風格，鏡像 `progress_events.py`；可 import `tool_errors` 做錯誤投影）。

### 3.1 規範 record（一份事實，OTel 命名）
```python
@dataclass(frozen=True)
class ToolCallEvent:
    """一次工具調用的規範事實（屬性名對齊 OTel gen_ai）。"""
    call_id: str                              # ← gen_ai.tool.call.id
    tool_name: str                            # ← gen_ai.tool.name
    arguments: dict                           # ← gen_ai.tool.call.arguments
    status: str | None = None                 # "ok" | "error"（span status）
    error_class: str | None = None            # ← error.type（沿用 tool_errors 五類）
    duration_ms: float | None = None          # ← span duration
    result_text: str | None = None            # 模型面/人面共用結果文字
    operation: str = "execute_tool"           # ← gen_ai.operation.name

    def to_result_sse(self) -> dict: ...       # 人面投影（見 3.2）
```

### 3.2 兩個投影
- **人面投影（SSE，wire 凍結 + additive）**：
  ```python
  def to_result_sse(self) -> dict:
      ev = {"type": "tool_result", "id": self.call_id,
            "result": self.result_text, "status": self.status_wire()}  # 'success'|'error'
      if self.error_class:   ev["error_class"] = self.error_class      # additive
      if self.duration_ms is not None: ev["duration_ms"] = self.duration_ms  # additive
      return ev
  ```
  註：wire 的 `status` 值維持既有 `"success"/"error"`（前端字串比對）；record 內部語意用 OTel 的 `ok/error`，由 `status_wire()` 轉換。
- **模型面投影（結構化錯誤）**：沿用既有 `tool_errors.build_tool_error(error_class, ...)`（本就是 `result_text`）；P1 只多提供 `tool_errors.extract_error_class(text) -> str|None`（前綴偵測後取第一個 key），讓 record 能把類別抬成一級屬性。**模型面 JSON 內容不變**（不破壞既有模型自我修正行為）。

### 3.3 薄 builder（start/stream 只知道部分資訊時用）
```python
def tool_start_event(*, call_id, tool, args) -> dict:
    return {"type": "tool_start", "id": call_id, "tool": tool, "args": args}
def tool_stream_event(*, call_id, chunk) -> dict:
    return {"type": "tool_stream", "id": call_id, "chunk": chunk}
def tool_error_event(*, call_id, message) -> dict:
    return {"type": "error", "id": call_id, "message": message}
```
外加 wire 型別常量（`TOOL_START="tool_start"` …）供 guard 與 handler 引用，成為**事件型別的單一定義處**（補上「無註冊表」的缺口）。

---

## 4. OTel GenAI semconv 對照表（借命名、不引 SDK）

| Touka 封套屬性 | OTel GenAI 屬性 | 備註 |
|---|---|---|
| `operation="execute_tool"` | `gen_ai.operation.name` | 工具執行操作名（span 名 `execute_tool {tool}`） |
| `tool_name` | `gen_ai.tool.name` | |
| `call_id` | `gen_ai.tool.call.id` | |
| `arguments` | `gen_ai.tool.call.arguments` | 敏感、OTel 標為 opt-in；本地事件本就帶 |
| `result_text` | `gen_ai.tool.call.result` | 同上 |
| `status`(ok/error) | span status | wire 投影成 `success/error` |
| `error_class` | `error.type` | 沿用 `tool_errors` 五類（validation/permission/not_found/timeout/upstream/internal） |
| `duration_ms` | span duration／`gen_ai.client.operation.duration` | P1 新增量測 |
| （回合層）`tokens` | `gen_ai.usage.input_tokens`/`output_tokens` | 已由 `token_usage_event` 承載；P1 不擴入工具事件 |
| （回合層）`finish_reason` | `gen_ai.response.finish_reasons` | 回合層；P1 不擴入工具事件 |

> 未來若真要接外部觀測（P4），這套命名可直接映射到 OTel exporter，屆時免改事件生產端。

---

## 5. TDD 工作拆解（紅 → 綠；每步獨立可回退）

### P1-A　封套 SSOT 模組（純測試 → 實作）
- **RED** `tests/unit/test_tool_events.py`（鏡像 `test_progress_events.py`）：
  - `tool_start_event/tool_stream_event/tool_error_event` 精確 dict 形狀（含 `type/id/...`）。
  - `ToolCallEvent.to_result_sse()`：成功 → `{type:tool_result,id,result,status:'success'}`；失敗 → status`'error'`＋additive `error_class`＋`duration_ms`。
  - **wire 凍結斷言**：成功事件的鍵集**恰為** `{type,id,result,status}`（無 duration 時不得多鍵）；有 duration/error_class 時才多。
  - 純函式（每次新 dict）。
  - `tool_errors.extract_error_class()`：對 `build_tool_error(...)` 產物回傳正確類別；對一般字串回 `None`。（加進既有 `tests/unit/test_tool_errors_structured.py`。）
- **GREEN**：實作 `src/utils/tool_events.py`＋`tool_errors.extract_error_class`。

### P1-B　tool_executor 走 SSOT ＋ 計時（紅 → 綠）
- **RED** `tests/proxy/test_tool_events_emit.py`（新）＋沿用 `tests/proxy/test_progress_emit.py` 的排空佇列手法：
  - **回歸**：`tool_start`/`tool_result` 的既有 wire 欄位**完全不變**（現有測試須全綠）。
  - **新增**：成功 `tool_result` 帶 `duration_ms`（float、≥0）。
  - **新增**：失敗（含 DENY 路徑）`tool_result` 帶 `error_class`，且等於對應 `tool_errors` 類別（如 DENY → `permission_denied`）。
  - `tool_stream`（terminal）經 `tool_events.tool_stream_event` 產生、形狀不變。
- **GREEN**：
  - `tool_executor.py`：把 233 / 216-218 / 358-365 / 378-379 的字面量換成 SSOT builder／`ToolCallEvent.to_result_sse()`。
  - **計時**：在實際 dispatch 前後取 monotonic 時鐘算 `duration_ms`。**先檢查 `src/utils/time_utils.py` 是否已有 monotonic helper；若無，新增 `monotonic_ms()` 到時間 SSOT**（避免直接 `time.perf_counter()` 觸發 lint_ssot／時間 SSOT 規則；wall-clock epoch 會因校時倒退，不可用於 duration）。
  - `terminal.py:239`、`interaction.py:44` 改用 builder（interaction 的 `ask_user_request` 非工具生命週期，僅示範性納入常量，**形狀不變**）。

### P1-C　事件一致性 guard（新 guard #38）
- **內容** `scripts/guard_tool_events.py`：AST 靜態掃描 `ToukaProxy/src/`，若在 `src/utils/tool_events.py`（SSOT）**以外**出現 `{"type": "tool_start"/"tool_result"/"tool_stream", ...}` 的**原始字面量**即失敗——強制未來新事件走封套。
  - 範圍限這三個**無歧義**的 `tool_*` 型別（`"error"` 太泛、易誤判，**刻意不納入**，改由文件約定＋builder 提供；guard 註解寫明此取捨）。
  - 允許清單：SSOT 模組本身＋ `tests/`。
- **RED**：guard 自測（inline fixture：一段含原始字面量的字串應被判失敗；走 builder 的應通過）。
- **GREEN**：實作 guard；註冊進 `scripts/guard_all.py` 的 `tasks`（新增一筆 `Backend Tool-Event Envelope Guard [Guard #38]`）；**更新守衛總數引用 37→38**（guard_all 輸出／HANDOVER 健康行）。

### P1-D　中央 SSE 序列化 helper（**選配、可獨立略過**）
- **內容**：`sse_line(event) -> str`（`f"data: {json.dumps(event, ensure_ascii=False)}\n\n"`），消掉三 handler 的重複硬編。
- **RED**：helper 單測（含非 ASCII 不被轉義）。
- **GREEN**：`claude/gemini/openai_handler` 改用 helper。
- 標記選配：核心價值在封套（A-C）；此項是附帶 SSOT 收斂，風險低但可延後，不阻擋主線。

---

## 6. 驗證關卡（每步）
- `ruff check .`＋`python scripts/lint_ssot.py`（含新 monotonic helper 的時間 SSOT 合規）。
- `python scripts/guard_all.py` → **38/38**（新 guard 綠）。
- `uv run python -m pytest`（全綠；新增 A/B/C 測試；既有 emit 測試回歸不變）。
- `npm --prefix ToukaChat run test`（vitest）＋ `npm --prefix ToukaChat run lint`：**應與現況一致**——前端零改動，此步是**凍結契約的證明**（若 vitest 變動＝wire 被破壞，立即回退）。

## 7. eval 影響（用 P0 的尺確認無退化）
- P1 是**遙測管線**，不動工具 schema／選擇邏輯 → `tests/tool_eval/` 離線套件（pass^k／abstention／wrong_tool／hallucination）**數字應完全不變**。
- 動作：跑一次離線 eval 套件確認無退化；**LIVE 不需重錄**（schema 未變）。這是「先有尺、改完再量」的收尾檢查。

## 8. 風險與明確「不做」
- **風險：中**（碰事件流）。緩解：①wire 欄位凍結、前端零改；②排空佇列的回歸測試；③新屬性一律 additive（前端忽略未知鍵）；④guard 防未來漂移；⑤每步可獨立回退。
- **不做**：改 wire 欄位名（→P3）；外部 OTel exporter／spans（→P4）；回合層 tokens/finish_reason 塞進工具事件；動 `touka_progress`／`log`／`touka_status`；把 `"error"` 泛型別納入 guard（易誤判）。

## 9. 文件與 ADR
- 已正式收斂為 **[ADR-0122](../adr/0122-tool-event-telemetry-envelope.md)**（延續 0119~0121 工具系列；P1 動 runtime 事件流，比純測試層的 P0 適合立 ADR）。
- 同步：`tool-observability-and-reliability.md` §6 Phase 1 標 ✅＋量測；`tests/tool_eval/README.md`（若 eval 有新對照）；`HANDOVER.md`。

## 10. 待明示
- 本計畫**待維護者審核**。核可後依 P1-A→B→C（→D 選配）逐步 TDD 實作，每步過全套關卡，commit 由維護者執行。
- 可討論的取捨點：①P1-D 是否納入本輪；②`duration_ms` 是否也附到成功事件之外的 `log`（預設否）；③guard 是否連 `"error"` 泛型別一起管（預設否，理由見 P1-C）。
